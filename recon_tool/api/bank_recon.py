import json
import re

import frappe
import requests
from frappe import _
from frappe.utils import add_days, cint, flt, getdate


PAYMENT_DOCTYPES = (
	"Payment Entry",
	"Journal Entry",
	"Sales Invoice",
	"Purchase Invoice",
	"Expense Claim",
)
PARTY_DOCTYPES = ("Customer", "Supplier", "Employee")
AUTO_MATCH_LIMIT = 200
INTERNAL_TRANSFER_KEYWORDS = (
	"SELF",
	"TRANSFER",
	"IMPS TO OWN",
	"FUND TRANSFER",
	"SWEEP",
	"BANK TRANSFER",
	"TRANSFER BETWEEN ACCOUNTS",
)
CONTRA_KEYWORDS = ("CASH DEPOSIT", "CASH WITHDRAWAL", "ATM", "CASH")


def _as_json(value, default=None):
	if value in (None, ""):
		return default
	if isinstance(value, str):
		return json.loads(value)
	return value


def _check_perm(doctype, ptype="read"):
	if not frappe.has_permission(doctype, ptype=ptype):
		frappe.throw(_("Not permitted to {0} {1}").format(ptype, doctype), frappe.PermissionError)


def _bank_account_context(bank_account):
	_check_perm("Bank Account", "read")
	row = frappe.db.get_value(
		"Bank Account",
		bank_account,
		["name", "account", "company", "account_name"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("Bank Account {0} not found").format(bank_account))
	if not row.account:
		frappe.throw(_("Bank Account {0} is not linked to a GL Account").format(bank_account))
	return row


def _transaction_amount(transaction):
	return abs(flt(transaction.get("deposit")) - flt(transaction.get("withdrawal")))


def _transaction_direction(transaction):
	return "Receive" if flt(transaction.get("deposit")) > 0 else "Pay"


def _gl_balance(account, to_date=None, before_date=None):
	_check_perm("GL Entry", "read")
	values = {"account": account}
	where = ["account = %(account)s", "is_cancelled = 0"]
	if before_date:
		values["before_date"] = before_date
		where.append("posting_date < %(before_date)s")
	elif to_date:
		values["to_date"] = to_date
		where.append("posting_date <= %(to_date)s")
	row = frappe.db.sql(
		f"""
		SELECT COALESCE(SUM(debit - credit), 0) AS balance
		FROM `tabGL Entry`
		WHERE {' AND '.join(where)}
		""",
		values,
		as_dict=True,
	)[0]
	return flt(row.balance)


def _normalize(text):
	return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _tokens(text):
	return {token for token in _normalize(text).split() if len(token) >= 3}


def _extract_refs(text):
	patterns = (
		r"\b(?:UTR|NEFT|IMPS|RTGS|UPI|CHQ|CHEQUE|REF|RRN)[:\-/\s]*([A-Z0-9]{6,})\b",
		r"\b(?:INV|BILL|SI|PI)[:\-/\s]*([A-Z0-9\-/]{4,})\b",
		r"\b([A-Z]{2,}\d{4,}[A-Z0-9\-/]*)\b",
		r"\b(\d{8,})\b",
	)
	refs = set()
	for pattern in patterns:
		for match in re.findall(pattern, text or "", flags=re.I):
			refs.add(match.upper().strip())
	return refs


def _date_gap(a, b):
	if not a or not b:
		return 999
	return abs((getdate(a) - getdate(b)).days)


def _score_candidate(transaction, candidate):
	txn_amount = _transaction_amount(transaction)
	candidate_amount = abs(flt(candidate.get("amount")))
	amount_diff = abs(txn_amount - candidate_amount)
	date_gap = _date_gap(transaction.get("date"), candidate.get("posting_date"))
	txn_text = " ".join(
		filter(
			None,
			[
				transaction.get("description"),
				transaction.get("reference_number"),
				transaction.get("transaction_id"),
				transaction.get("bank_party_name"),
			],
		)
	)
	candidate_text = " ".join(
		filter(
			None,
			[
				candidate.get("name"),
				candidate.get("party"),
				candidate.get("party_name"),
				candidate.get("reference"),
				candidate.get("remarks"),
			],
		)
	)
	txn_refs = _extract_refs(txn_text)
	candidate_refs = _extract_refs(candidate_text)
	overlap = _tokens(txn_text).intersection(_tokens(candidate_text))

	score = 0
	reasons = []
	if amount_diff == 0:
		score += 40
		reasons.append(_("exact amount"))
	elif txn_amount and amount_diff <= max(5, txn_amount * 0.02):
		score += 24
		reasons.append(_("similar amount"))
	elif txn_amount and amount_diff <= max(25, txn_amount * 0.08):
		score += 12
		reasons.append(_("near amount"))

	if date_gap == 0:
		score += 22
		reasons.append(_("same date"))
	elif date_gap <= 3:
		score += 16
		reasons.append(_("within 3 days"))
	elif date_gap <= 10:
		score += 7
		reasons.append(_("near date"))

	if txn_refs and candidate_refs and txn_refs.intersection(candidate_refs):
		score += 24
		reasons.append(_("reference match"))

	if overlap:
		score += min(14, len(overlap) * 4)
		reasons.append(_("party/narration match"))

	if candidate.get("party") and _normalize(candidate.get("party")) in _normalize(txn_text):
		score += 12
		reasons.append(_("party detected"))

	if candidate.get("doctype") in ("Sales Invoice", "Purchase Invoice") and candidate.get("name"):
		if candidate.get("name").upper() in txn_text.upper():
			score += 20
			reasons.append(_("invoice number detected"))

	return min(100, cint(score)), ", ".join(dict.fromkeys(reasons)) or _("possible match"), flt(amount_diff)


def _candidate_key(row):
	return f"{row.get('doctype')}::{row.get('name')}"


def _append_candidate(results, seen, row):
	key = _candidate_key(row)
	if key not in seen:
		seen.add(key)
		results.append(row)


def _conditions_for_search(alias, fields, search):
	if not search:
		return "", {}
	parts = []
	params = {}
	for idx, field in enumerate(fields):
		key = f"search_{alias}_{idx}"
		parts.append(f"{field} LIKE %({key})s")
		params[key] = f"%{search}%"
	return f" AND ({' OR '.join(parts)})", params


def _get_transaction_doc(name):
	_check_perm("Bank Transaction", "read")
	doc = frappe.get_doc("Bank Transaction", name)
	if doc.docstatus != 1:
		frappe.throw(_("Only submitted Bank Transactions can be reconciled"))
	return doc


def _validate_company_account(account, company, label=None):
	_check_perm("Account", "read")
	row = frappe.db.get_value(
		"Account",
		account,
		["name", "company", "is_group", "account_type"],
		as_dict=True,
	)
	if not row:
		frappe.throw(_("{0} {1} not found").format(label or _("Account"), account))
	if row.company != company:
		frappe.throw(_("{0} must belong to company {1}").format(label or _("Account"), company))
	if cint(row.is_group):
		frappe.throw(_("{0} must be a ledger account").format(label or _("Account")))
	return row


def _bank_transaction_duplicate_guard(bank_transaction, doctype, voucher_name=None):
	if not bank_transaction or not voucher_name:
		return
	exists = frappe.db.exists(
		"Bank Transaction Payments",
		{
			"parent": bank_transaction,
			"payment_document": doctype,
			"payment_entry": voucher_name,
		},
	)
	if exists:
		frappe.throw(_("{0} is already linked to this Bank Transaction").format(voucher_name))


def _text_for_detection(transaction):
	return " ".join(
		filter(
			None,
			[
				transaction.get("description"),
				transaction.get("reference_number"),
				transaction.get("transaction_id"),
				transaction.get("transaction_type"),
				transaction.get("bank_party_name"),
			],
		)
	).upper()


def _detect_actions(transaction):
	text = _text_for_detection(transaction)
	actions = []
	internal_hits = [word for word in INTERNAL_TRANSFER_KEYWORDS if word in text]
	contra_hits = [word for word in CONTRA_KEYWORDS if word in text]

	if internal_hits:
		score = 92 if any(word in text for word in ("SELF", "OWN", "BETWEEN ACCOUNTS")) else 82
		actions.append(
			{
				"action": "Internal Transfer",
				"score": score,
				"label": _("Suggested: Internal Transfer"),
				"reason": _("Narration contains {0}").format(", ".join(internal_hits[:3])),
			}
		)

	if contra_hits:
		score = 90 if any(word in text for word in ("CASH DEPOSIT", "CASH WITHDRAWAL", "ATM")) else 76
		actions.append(
			{
				"action": "Contra Entry",
				"score": score,
				"label": _("Suggested: Contra Entry"),
				"reason": _("Narration contains {0}").format(", ".join(contra_hits[:3])),
			}
		)

	if not actions:
		direction = _transaction_direction(transaction)
		actions.append(
			{
				"action": "Customer Receipt" if direction == "Receive" else "Supplier Payment",
				"score": 45,
				"label": _("Suggested: {0}").format(_("Customer Receipt") if direction == "Receive" else _("Supplier Payment")),
				"reason": _("Based on money direction"),
			}
		)

	actions.sort(key=lambda row: row["score"], reverse=True)
	return actions


@frappe.whitelist()
def get_bank_transactions(
	bank_account,
	from_date,
	to_date,
	status="unmatched",
	search="",
	start=0,
	page_length=50,
):
	_check_perm("Bank Transaction", "read")
	_bank_account_context(bank_account)
	start = cint(start)
	page_length = min(cint(page_length) or 50, 200)
	values = {
		"bank_account": bank_account,
		"from_date": from_date,
		"to_date": to_date,
		"start": start,
		"page_length": page_length,
	}
	where = [
		"bt.docstatus = 1",
		"bt.bank_account = %(bank_account)s",
		"bt.date BETWEEN %(from_date)s AND %(to_date)s",
	]
	if status in ("unmatched", "matched"):
		where.append("bt.unallocated_amount > 0" if status == "unmatched" else "bt.unallocated_amount <= 0")
	if search:
		values["search"] = f"%{search}%"
		where.append(
			"""(
				bt.name LIKE %(search)s
				OR bt.description LIKE %(search)s
				OR bt.reference_number LIKE %(search)s
				OR bt.transaction_id LIKE %(search)s
				OR bt.bank_party_name LIKE %(search)s
				OR CAST(bt.deposit AS CHAR) LIKE %(search)s
				OR CAST(bt.withdrawal AS CHAR) LIKE %(search)s
			)"""
		)

	where_sql = " AND ".join(where)
	total = frappe.db.sql(f"SELECT COUNT(*) FROM `tabBank Transaction` bt WHERE {where_sql}", values)[0][0]
	transactions = frappe.db.sql(
		f"""
		SELECT
			bt.name,
			bt.date,
			bt.description,
			bt.deposit,
			bt.withdrawal,
			bt.reference_number,
			bt.transaction_id,
			bt.transaction_type,
			bt.bank_party_name,
			bt.bank_account,
			ba.account AS bank_gl_account,
			bt.allocated_amount,
			bt.unallocated_amount,
			bt.status
		FROM `tabBank Transaction` bt
		INNER JOIN `tabBank Account` ba ON ba.name = bt.bank_account
		WHERE {where_sql}
		ORDER BY bt.date DESC, bt.creation DESC
		LIMIT %(page_length)s OFFSET %(start)s
		""",
		values,
		as_dict=True,
	)
	for row in transactions:
		row.amount = _transaction_amount(row)
		row.direction = _transaction_direction(row)
		row.is_matched = flt(row.unallocated_amount) <= 0
		row.detected_actions = _detect_actions(row)

	summary = frappe.db.sql(
		"""
		SELECT
			COALESCE(SUM(deposit - withdrawal), 0) AS bank_balance,
			COALESCE(SUM(allocated_amount), 0) AS allocated_total,
			COALESCE(SUM(unallocated_amount), 0) AS unallocated_total,
			SUM(CASE WHEN unallocated_amount > 0 THEN 1 ELSE 0 END) AS unmatched_count
		FROM `tabBank Transaction`
		WHERE docstatus = 1
			AND bank_account = %(bank_account)s
			AND date BETWEEN %(from_date)s AND %(to_date)s
		""",
		values,
		as_dict=True,
	)[0]

	return {
		"transactions": transactions,
		"total": total,
		"bank_balance": flt(summary.bank_balance),
		"allocated_total": flt(summary.allocated_total),
		"unallocated_total": flt(summary.unallocated_total),
		"unmatched_count": cint(summary.unmatched_count),
	}


@frappe.whitelist()
def get_balance_summary(bank_account, from_date, to_date, statement_closing_balance=None):
	ctx = _bank_account_context(bank_account)
	opening_balance = _gl_balance(ctx.account, before_date=from_date)
	erp_closing_balance = _gl_balance(ctx.account, to_date=to_date)
	statement_closing_balance = flt(statement_closing_balance) if statement_closing_balance not in (None, "") else None
	difference = statement_closing_balance - erp_closing_balance if statement_closing_balance is not None else None
	return {
		"bank_account": ctx.name,
		"gl_account": ctx.account,
		"account_opening_balance": opening_balance,
		"erp_closing_balance": erp_closing_balance,
		"statement_closing_balance": statement_closing_balance,
		"difference": difference,
	}


def _bank_transaction_exists(bank_account, transaction_id=None, reference_number=None, date=None, deposit=0, withdrawal=0):
	filters = {"bank_account": bank_account}
	if transaction_id:
		filters["transaction_id"] = transaction_id
		existing = frappe.db.exists("Bank Transaction", filters)
		if existing:
			return existing
	if reference_number:
		filters.pop("transaction_id", None)
		filters.update(
			{
				"reference_number": reference_number,
				"date": date,
				"deposit": flt(deposit),
				"withdrawal": flt(withdrawal),
			}
		)
		return frappe.db.exists("Bank Transaction", filters)
	return None


def _create_bank_transaction(bank_account, row):
	_check_perm("Bank Transaction", "create")
	date = row.get("date") or row.get("transaction_date") or row.get("value_date")
	deposit = flt(row.get("deposit") or row.get("credit") or 0)
	withdrawal = flt(row.get("withdrawal") or row.get("debit") or 0)
	transaction_id = row.get("transaction_id") or row.get("id") or row.get("utr")
	reference_number = row.get("reference_number") or row.get("reference") or row.get("utr")
	if not date:
		frappe.throw(_("SBI transaction row is missing transaction date"))
	if deposit <= 0 and withdrawal <= 0:
		frappe.throw(_("SBI transaction row must have deposit or withdrawal amount"))
	existing = _bank_transaction_exists(bank_account, transaction_id, reference_number, date, deposit, withdrawal)
	if existing:
		return {"name": existing, "created": False}

	doc = frappe.new_doc("Bank Transaction")
	doc.bank_account = bank_account
	doc.date = date
	doc.deposit = deposit
	doc.withdrawal = withdrawal
	doc.description = row.get("description") or row.get("narration") or row.get("remarks")
	doc.reference_number = reference_number
	doc.transaction_id = transaction_id
	doc.transaction_type = row.get("transaction_type") or row.get("type")
	doc.bank_party_name = row.get("bank_party_name") or row.get("party_name")
	doc.insert()
	doc.submit()
	return {"name": doc.name, "created": True}


def _read_path(data, path, default=None):
	if not path:
		return data
	value = data
	for part in str(path).split("."):
		if isinstance(value, dict):
			value = value.get(part)
		elif isinstance(value, list) and part.isdigit():
			value = value[cint(part)] if cint(part) < len(value) else None
		else:
			return default
		if value is None:
			return default
	return value


def _map_sbi_transaction(row, field_map):
	if not field_map:
		return row
	return {target: _read_path(row, source) for target, source in field_map.items()}


def _fetch_sbi_transactions(bank_account, from_date, to_date):
	"""Fetch live SBI transactions using site_config.json mapping.

	Expected site_config key: sbi_bank_api with endpoint, optional method,
	headers, token, account_number_by_bank_account, transactions_path,
	closing_balance_path and field_map.
	"""
	# SBI sync integration disabled.
	frappe.throw(_("SBI bank sync has been disabled."))
	return {"transactions": [], "closing_balance": None}


@frappe.whitelist()
def sync_sbi_bank_transactions(bank_account, from_date, to_date):
	# SBI sync endpoint disabled
	frappe.throw(_("SBI bank sync endpoint has been disabled."))


@frappe.whitelist()
def detect_transaction_type(bank_transaction=None, transaction=None):
	if bank_transaction:
		doc = _get_transaction_doc(bank_transaction)
		transaction = doc.as_dict()
	else:
		transaction = _as_json(transaction, {}) or {}
	if not transaction:
		frappe.throw(_("Provide a Bank Transaction or transaction payload"))
	actions = _detect_actions(transaction)
	return {
		"bank_transaction": transaction.get("name"),
		"actions": actions,
		"primary_action": actions[0] if actions else None,
	}


@frappe.whitelist()
def fetch_parties(doctype=None, txt="", searchfield="name", start=0, page_len=20, filters=None):
	filters = _as_json(filters, {}) or {}
	party_type = filters.get("party_type") or doctype
	if party_type not in PARTY_DOCTYPES:
		return []
	_check_perm(party_type, "read")
	meta = frappe.get_meta(party_type)
	search_fields = ["name"]
	for field in ("customer_name", "supplier_name", "employee_name"):
		if meta.has_field(field):
			search_fields.append(field)
	enabled_condition = ""
	if meta.has_field("disabled"):
		enabled_condition = "AND IFNULL(disabled, 0) = 0"
	elif party_type == "Employee" and meta.has_field("status"):
		enabled_condition = "AND status = 'Active'"
	label_field = next((field for field in ("customer_name", "supplier_name", "employee_name") if meta.has_field(field)), "name")
	values = {"txt": f"%{txt}%", "start": cint(start), "page_len": cint(page_len) or 20}
	conditions = " OR ".join(f"`{field}` LIKE %(txt)s" for field in search_fields)
	return frappe.db.sql(
		f"""
		SELECT name, `{label_field}`
		FROM `tab{party_type}`
		WHERE ({conditions})
			{enabled_condition}
		ORDER BY modified DESC
		LIMIT %(page_len)s OFFSET %(start)s
		""",
		values,
	)


@frappe.whitelist()
def fetch_open_invoices(party_type=None, party=None, company=None, search="", start=0, page_length=20):
	page_length = min(cint(page_length) or 20, 100)
	results = []
	if party_type in (None, "", "Customer"):
		results.extend(
			_get_invoice_candidates(
				"Sales Invoice",
				party_field="customer",
				party=party if party_type == "Customer" else None,
				company=company,
				search=search,
				start=start,
				page_length=page_length,
			)
		)
	if party_type in (None, "", "Supplier"):
		results.extend(
			_get_invoice_candidates(
				"Purchase Invoice",
				party_field="supplier",
				party=party if party_type == "Supplier" else None,
				company=company,
				search=search,
				start=start,
				page_length=page_length,
			)
		)
	return {"invoices": results[:page_length]}


def _get_invoice_candidates(doctype, party_field, party=None, company=None, search="", start=0, page_length=20):
	_check_perm(doctype, "read")
	values = {"start": cint(start), "page_length": cint(page_length)}
	where = ["docstatus = 1", "outstanding_amount > 0"]
	if party:
		values["party"] = party
		where.append(f"{party_field} = %(party)s")
	if company:
		values["company"] = company
		where.append("company = %(company)s")
	if search:
		values["search"] = f"%{search}%"
		where.append(f"(name LIKE %(search)s OR {party_field} LIKE %(search)s OR remarks LIKE %(search)s)")
	party_label = "customer_name" if doctype == "Sales Invoice" else "supplier_name"
	return frappe.db.sql(
		f"""
		SELECT
			'{doctype}' AS doctype,
			name,
			posting_date,
			{party_field} AS party,
			{party_label} AS party_name,
			outstanding_amount AS amount,
			grand_total,
			remarks,
			NULL AS reference
		FROM `tab{doctype}`
		WHERE {' AND '.join(where)}
		ORDER BY posting_date DESC, outstanding_amount DESC
		LIMIT %(page_length)s OFFSET %(start)s
		""",
		values,
		as_dict=True,
	)


@frappe.whitelist()
def get_reconciliation_suggestions(
	bank_account=None,
	bank_transaction=None,
	amount=None,
	transaction_date=None,
	description="",
	reference_number="",
	search="",
	start=0,
	page_length=50,
):
	_check_perm("Bank Transaction", "read")
	if bank_transaction:
		transaction = frappe.get_doc("Bank Transaction", bank_transaction).as_dict()
	else:
		transaction = {
			"name": "",
			"bank_account": bank_account,
			"date": transaction_date,
			"deposit": flt(amount),
			"withdrawal": 0,
			"description": description,
			"reference_number": reference_number,
		}

	ctx = _bank_account_context(transaction.get("bank_account") or bank_account)
	page_length = min(cint(page_length) or 50, 100)
	candidates = []
	seen = set()
	for rows in (
		_payment_entry_candidates(ctx, transaction, search),
		_journal_entry_candidates(ctx, transaction, search),
		_invoice_suggestion_candidates("Sales Invoice", "customer", ctx, transaction, search),
		_invoice_suggestion_candidates("Purchase Invoice", "supplier", ctx, transaction, search),
		_expense_claim_candidates(ctx, transaction, search),
	):
		for row in rows:
			_append_candidate(candidates, seen, row)

	scored = []
	for candidate in candidates:
		score, comment, amount_difference = _score_candidate(transaction, candidate)
		candidate.score = score
		candidate.comment = comment
		candidate.amount_difference = amount_difference
		candidate.posting_date = str(candidate.get("posting_date") or "")
		scored.append(candidate)
	scored.sort(key=lambda row: (row.score, -row.amount_difference), reverse=True)
	start = cint(start)
	return {
		"suggestions": scored[start : start + page_length],
		"total": len(scored),
		"references": sorted(_extract_refs(" ".join([description or "", reference_number or ""]))),
	}


def _amount_window(transaction, percent=0.08, floor=10):
	amount = _transaction_amount(transaction)
	if not amount:
		return 0, 0
	delta = max(floor, amount * percent)
	return max(0, amount - delta), amount + delta


def _payment_entry_candidates(ctx, transaction, search=""):
	_check_perm("Payment Entry", "read")
	low, high = _amount_window(transaction)
	values = {
		"company": ctx.company,
		"bank_account": ctx.name,
		"gl_account": ctx.account,
		"low": low,
		"high": high,
		"from_date": add_days(transaction.get("date"), -15),
		"to_date": add_days(transaction.get("date"), 15),
	}
	search_sql, search_params = _conditions_for_search("pe", ["pe.name", "pe.party", "pe.reference_no", "pe.remarks"], search)
	values.update(search_params)
	return frappe.db.sql(
		f"""
		SELECT DISTINCT
			'Payment Entry' AS doctype,
			pe.name,
			pe.posting_date,
			pe.party,
			pe.party_name,
			pe.reference_no AS reference,
			pe.remarks,
			pe.unallocated_amount AS amount
		FROM `tabPayment Entry` pe
		LEFT JOIN `tabGL Entry` gle
			ON gle.voucher_type = 'Payment Entry'
			AND gle.voucher_no = pe.name
			AND gle.account = %(gl_account)s
			AND gle.is_cancelled = 0
		WHERE pe.docstatus = 1
			AND pe.company = %(company)s
			AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND IFNULL(pe.unallocated_amount, 0) > 0
			AND ABS(pe.unallocated_amount) BETWEEN %(low)s AND %(high)s
			AND (pe.bank_account = %(bank_account)s OR gle.name IS NOT NULL)
			{search_sql}
		ORDER BY pe.posting_date DESC
		LIMIT 40
		""",
		values,
		as_dict=True,
	)


def _invoice_suggestion_candidates(doctype, party_field, ctx, transaction, search=""):
	_check_perm(doctype, "read")
	low, high = _amount_window(transaction)
	values = {
		"company": ctx.company,
		"low": low,
		"high": high,
		"from_date": add_days(transaction.get("date"), -180),
		"to_date": add_days(transaction.get("date"), 30),
	}
	search_sql, search_params = _conditions_for_search(
		"inv",
		["name", party_field, "remarks"],
		search,
	)
	values.update(search_params)
	party_label = "customer_name" if doctype == "Sales Invoice" else "supplier_name"
	return frappe.db.sql(
		f"""
		SELECT
			'{doctype}' AS doctype,
			name,
			posting_date,
			{party_field} AS party,
			{party_label} AS party_name,
			outstanding_amount AS amount,
			grand_total,
			remarks,
			NULL AS reference
		FROM `tab{doctype}`
		WHERE docstatus = 1
			AND company = %(company)s
			AND outstanding_amount > 0
			AND posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND outstanding_amount BETWEEN %(low)s AND %(high)s
			{search_sql}
		ORDER BY posting_date DESC, outstanding_amount DESC
		LIMIT 40
		""",
		values,
		as_dict=True,
	)


def _journal_entry_candidates(ctx, transaction, search=""):
	_check_perm("Journal Entry", "read")
	low, high = _amount_window(transaction)
	values = {
		"company": ctx.company,
		"gl_account": ctx.account,
		"low": low,
		"high": high,
		"from_date": add_days(transaction.get("date"), -15),
		"to_date": add_days(transaction.get("date"), 15),
	}
	search_sql, search_params = _conditions_for_search("je", ["je.name", "je.cheque_no", "je.user_remark", "je.remark"], search)
	values.update(search_params)
	return frappe.db.sql(
		f"""
		SELECT
			'Journal Entry' AS doctype,
			je.name,
			je.posting_date,
			NULL AS party,
			NULL AS party_name,
			je.cheque_no AS reference,
			COALESCE(je.user_remark, je.remark) AS remarks,
			ABS(SUM(gle.debit - gle.credit)) AS amount
		FROM `tabJournal Entry` je
		INNER JOIN `tabGL Entry` gle
			ON gle.voucher_type = 'Journal Entry'
			AND gle.voucher_no = je.name
			AND gle.account = %(gl_account)s
			AND gle.is_cancelled = 0
		WHERE je.docstatus = 1
			AND je.company = %(company)s
			AND je.posting_date BETWEEN %(from_date)s AND %(to_date)s
			{search_sql}
		GROUP BY je.name
		HAVING amount BETWEEN %(low)s AND %(high)s
		ORDER BY je.posting_date DESC
		LIMIT 30
		""",
		values,
		as_dict=True,
	)


def _expense_claim_candidates(ctx, transaction, search=""):
	if not frappe.db.exists("DocType", "Expense Claim"):
		return []
	_check_perm("Expense Claim", "read")
	low, high = _amount_window(transaction)
	values = {
		"company": ctx.company,
		"low": low,
		"high": high,
		"from_date": add_days(transaction.get("date"), -30),
		"to_date": add_days(transaction.get("date"), 30),
	}
	search_sql, search_params = _conditions_for_search("ec", ["ec.name", "ec.employee", "ec.employee_name", "ec.remark"], search)
	values.update(search_params)
	return frappe.db.sql(
		f"""
		SELECT
			'Expense Claim' AS doctype,
			ec.name,
			ec.posting_date,
			ec.employee AS party,
			ec.employee_name AS party_name,
			NULL AS reference,
			ec.remark AS remarks,
			ec.grand_total AS amount
		FROM `tabExpense Claim` ec
		WHERE ec.docstatus = 1
			AND ec.company = %(company)s
			AND ec.status NOT IN ('Paid', 'Rejected', 'Cancelled')
			AND ec.posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND ec.grand_total BETWEEN %(low)s AND %(high)s
			{search_sql}
		ORDER BY ec.posting_date DESC
		LIMIT 20
		""",
		values,
		as_dict=True,
	)


@frappe.whitelist()
def reconcile_transaction(bank_transaction, vouchers):
	_check_perm("Bank Transaction", "write")
	vouchers = _as_json(vouchers, []) or []
	if not vouchers:
		frappe.throw(_("Select at least one voucher"))
	transaction = _get_transaction_doc(bank_transaction)
	existing = {(row.payment_document, row.payment_entry) for row in transaction.get("payment_entries")}
	payload = []
	for voucher in vouchers:
		doctype = voucher.get("doctype") or voucher.get("payment_doctype")
		name = voucher.get("name") or voucher.get("payment_name")
		if doctype not in PAYMENT_DOCTYPES:
			frappe.throw(_("Unsupported voucher type {0}").format(doctype))
		_check_perm(doctype, "read")
		if not frappe.db.exists(doctype, name):
			frappe.throw(_("{0} {1} not found").format(doctype, name))
		if (doctype, name) not in existing:
			payload.append({"payment_doctype": doctype, "payment_name": name})
	if not payload:
		return {"status": "ok", "message": _("Already reconciled"), "transaction": transaction.name}

	transaction.add_payment_entries(payload)
	transaction.validate_duplicate_references()
	transaction.allocate_payment_entries()
	transaction.update_allocated_amount()
	transaction.set_status()
	transaction.save()
	frappe.db.commit()
	return {
		"status": "ok",
		"transaction": transaction.name,
		"allocated_amount": transaction.allocated_amount,
		"unallocated_amount": transaction.unallocated_amount,
		"is_reconciled": flt(transaction.unallocated_amount) <= 0,
	}


@frappe.whitelist()
def auto_reconcile(bank_account, from_date, to_date, threshold=86, transaction_names=None):
	_check_perm("Bank Transaction", "write")
	threshold = cint(threshold) or 86
	transaction_names = _as_json(transaction_names, None)
	values = {
		"bank_account": bank_account,
		"from_date": from_date,
		"to_date": to_date,
		"limit": AUTO_MATCH_LIMIT,
	}
	where = [
		"docstatus = 1",
		"bank_account = %(bank_account)s",
		"date BETWEEN %(from_date)s AND %(to_date)s",
		"unallocated_amount > 0",
	]
	if transaction_names:
		names = [frappe.db.escape(name) for name in transaction_names]
		where.append(f"name IN ({', '.join(names)})")
	transactions = frappe.db.sql(
		f"""
		SELECT name, date, description, reference_number, transaction_id, bank_party_name,
			deposit, withdrawal, bank_account, unallocated_amount
		FROM `tabBank Transaction`
		WHERE {' AND '.join(where)}
		ORDER BY date ASC
		LIMIT %(limit)s
		""",
		values,
		as_dict=True,
	)

	matched = []
	review_queue = []
	errors = []
	used_vouchers = set()
	for transaction in transactions:
		try:
			suggestions = get_reconciliation_suggestions(bank_transaction=transaction.name, page_length=8).get("suggestions", [])
			available = [s for s in suggestions if _candidate_key(s) not in used_vouchers]
			exact = [s for s in available if cint(s.score) >= threshold and flt(s.amount_difference) == 0]
			if exact:
				best = exact[0]
				result = reconcile_transaction(transaction.name, [{"doctype": best.doctype, "name": best.name}])
				if result.get("is_reconciled"):
					matched.append({"bank_transaction": transaction.name, "doctype": best.doctype, "name": best.name, "score": best.score})
					used_vouchers.add(_candidate_key(best))
				else:
					review_queue.append({"bank_transaction": transaction.name, "suggestions": available[:3]})
			elif available:
				review_queue.append({"bank_transaction": transaction.name, "suggestions": available[:3]})
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), "Recon Tool Auto Reconcile")
			errors.append({"bank_transaction": transaction.name, "error": str(exc)})
	return {
		"matched_count": len(matched),
		"matched": matched,
		"review_queue": review_queue,
		"errors": errors,
		"threshold": threshold,
	}


@frappe.whitelist()
def create_voucher(
	voucher_type,
	bank_account,
	amount,
	date,
	entry_type=None,
	party_type=None,
	party=None,
	mode=None,
	narration=None,
	reference_number=None,
	counterparty_account=None,
	bank_transaction=None,
):
	amount = flt(amount)
	if amount <= 0:
		frappe.throw(_("Amount must be greater than zero"))
	if voucher_type not in PAYMENT_DOCTYPES:
		frappe.throw(_("Unsupported voucher type {0}").format(voucher_type))
	ctx = _bank_account_context(bank_account)
	transaction = frappe.get_doc("Bank Transaction", bank_transaction) if bank_transaction else None
	bank_direction = _transaction_direction(transaction.as_dict()) if transaction else "Pay"
	direction = entry_type if entry_type in ("Receive", "Pay") else bank_direction

	if voucher_type == "Payment Entry":
		name = _create_payment_entry(
			ctx,
			amount,
			date,
			party_type,
			party,
			mode,
			narration,
			reference_number,
			direction,
			counterparty_account,
			bank_direction,
		)
	elif voucher_type == "Journal Entry":
		name = _create_journal_entry(ctx, amount, date, counterparty_account, narration, reference_number, direction, entry_type)
	elif voucher_type in ("Sales Invoice", "Purchase Invoice"):
		name = _create_invoice(voucher_type, ctx, amount, date, party, narration)
	else:
		frappe.throw(_("Create Expense Claim requires expense line details. Use Journal Entry for expense creation from this panel."))

	result = {"status": "ok", "doctype": voucher_type, "name": name}
	if bank_transaction and name:
		result["reconciliation"] = reconcile_transaction(bank_transaction, [{"doctype": voucher_type, "name": name}])
	return result


@frappe.whitelist()
def create_voucher_and_reconcile(**kwargs):
	return create_voucher(**kwargs)


@frappe.whitelist()
def create_internal_transfer(
	bank_transaction,
	from_bank_account,
	to_bank_account,
	amount,
	date,
	reference_number=None,
	mode=None,
	remarks=None,
):
	_check_perm("Payment Entry", "create")
	transaction = _get_transaction_doc(bank_transaction)
	amount = flt(amount)
	if amount <= 0:
		frappe.throw(_("Amount must be greater than zero"))
	if from_bank_account == to_bank_account:
		frappe.throw(_("From Bank Account and To Bank Account cannot be the same"))

	from_ctx = _bank_account_context(from_bank_account)
	to_ctx = _bank_account_context(to_bank_account)
	if from_ctx.company != to_ctx.company:
		frappe.throw(_("Both bank accounts must belong to the same company"))
	if transaction.bank_account not in (from_bank_account, to_bank_account):
		frappe.throw(_("Selected Bank Transaction must belong to either the source or target bank account"))

	if flt(transaction.withdrawal) > 0 and transaction.bank_account != from_bank_account:
		frappe.throw(_("For a withdrawal transaction, From Bank Account must be the selected bank account"))
	if flt(transaction.deposit) > 0 and transaction.bank_account != to_bank_account:
		frappe.throw(_("For a deposit transaction, To Bank Account must be the selected bank account"))

	duplicate = _find_duplicate_internal_transfer(
		from_ctx.account,
		to_ctx.account,
		from_ctx.company,
		amount,
		date,
		reference_number,
	)
	if duplicate:
		frappe.throw(_("Possible duplicate Internal Transfer already exists: {0}").format(duplicate))

	doc = frappe.new_doc("Payment Entry")
	doc.company = from_ctx.company
	doc.posting_date = date
	doc.payment_type = "Internal Transfer"
	doc.paid_from = from_ctx.account
	doc.paid_to = to_ctx.account
	doc.paid_amount = amount
	doc.received_amount = amount
	doc.reference_no = reference_number or "BANK-TRANSFER"
	doc.reference_date = date
	doc.mode_of_payment = mode
	doc.remarks = remarks
	doc.insert()
	doc.submit()

	result = reconcile_transaction(bank_transaction, [{"doctype": "Payment Entry", "name": doc.name}])
	return {
		"status": "ok",
		"doctype": "Payment Entry",
		"name": doc.name,
		"reconciliation": result,
	}


def _find_duplicate_internal_transfer(from_account, to_account, company, amount, date, reference_number=None):
	values = {
		"company": company,
		"from_account": from_account,
		"to_account": to_account,
		"amount": amount,
		"from_date": add_days(date, -3),
		"to_date": add_days(date, 3),
	}
	ref_sql = ""
	if reference_number:
		values["reference_number"] = reference_number
		ref_sql = "AND pe.reference_no = %(reference_number)s"
	rows = frappe.db.sql(
		f"""
		SELECT pe.name
		FROM `tabPayment Entry` pe
		WHERE pe.docstatus = 1
			AND pe.company = %(company)s
			AND pe.payment_type = 'Internal Transfer'
			AND pe.paid_from = %(from_account)s
			AND pe.paid_to = %(to_account)s
			AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s
			AND pe.paid_amount = %(amount)s
			{ref_sql}
		LIMIT 1
		""",
		values,
		as_dict=True,
	)
	return rows[0].name if rows else None


@frappe.whitelist()
def create_contra_entry(
	bank_transaction,
	debit_account,
	credit_account,
	amount,
	date,
	remark=None,
	reference_number=None,
):
	_check_perm("Journal Entry", "create")
	transaction = _get_transaction_doc(bank_transaction)
	ctx = _bank_account_context(transaction.bank_account)
	amount = flt(amount)
	if amount <= 0:
		frappe.throw(_("Amount must be greater than zero"))
	if debit_account == credit_account:
		frappe.throw(_("Debit Account and Credit Account cannot be the same"))

	_validate_company_account(debit_account, ctx.company, _("Debit Account"))
	_validate_company_account(credit_account, ctx.company, _("Credit Account"))
	if ctx.account not in (debit_account, credit_account):
		frappe.throw(_("One row of the Contra Entry must use the selected bank account GL"))

	duplicate = _find_duplicate_contra_entry(
		ctx.company,
		debit_account,
		credit_account,
		amount,
		date,
		reference_number,
	)
	if duplicate:
		frappe.throw(_("Possible duplicate Contra Entry already exists: {0}").format(duplicate))

	doc = frappe.new_doc("Journal Entry")
	doc.company = ctx.company
	doc.voucher_type = "Contra Entry"
	doc.posting_date = date
	doc.cheque_no = reference_number
	doc.cheque_date = date
	doc.user_remark = remark
	doc.append(
		"accounts",
		{
			"account": debit_account,
			"debit_in_account_currency": amount,
			"credit_in_account_currency": 0,
		},
	)
	doc.append(
		"accounts",
		{
			"account": credit_account,
			"debit_in_account_currency": 0,
			"credit_in_account_currency": amount,
		},
	)
	doc.insert()
	doc.submit()

	result = reconcile_transaction(bank_transaction, [{"doctype": "Journal Entry", "name": doc.name}])
	return {
		"status": "ok",
		"doctype": "Journal Entry",
		"name": doc.name,
		"reconciliation": result,
	}


def _find_duplicate_contra_entry(company, debit_account, credit_account, amount, date, reference_number=None):
	values = {
		"company": company,
		"debit_account": debit_account,
		"credit_account": credit_account,
		"amount": amount,
		"from_date": add_days(date, -3),
		"to_date": add_days(date, 3),
	}
	ref_sql = ""
	if reference_number:
		values["reference_number"] = reference_number
		ref_sql = "AND je.cheque_no = %(reference_number)s"
	rows = frappe.db.sql(
		f"""
		SELECT je.name
		FROM `tabJournal Entry` je
		INNER JOIN `tabJournal Entry Account` debit
			ON debit.parent = je.name
			AND debit.account = %(debit_account)s
			AND debit.debit_in_account_currency = %(amount)s
		INNER JOIN `tabJournal Entry Account` credit
			ON credit.parent = je.name
			AND credit.account = %(credit_account)s
			AND credit.credit_in_account_currency = %(amount)s
		WHERE je.docstatus = 1
			AND je.company = %(company)s
			AND je.voucher_type = 'Contra Entry'
			AND je.posting_date BETWEEN %(from_date)s AND %(to_date)s
			{ref_sql}
		LIMIT 1
		""",
		values,
		as_dict=True,
	)
	return rows[0].name if rows else None


@frappe.whitelist()
def auto_reconcile_internal_transfer(bank_account, from_date, to_date, threshold=88):
	_check_perm("Bank Transaction", "write")
	ctx = _bank_account_context(bank_account)
	threshold = cint(threshold) or 88
	transactions = frappe.db.sql(
		"""
		SELECT name, date, description, reference_number, transaction_id, transaction_type,
			bank_party_name, bank_account, deposit, withdrawal, unallocated_amount
		FROM `tabBank Transaction`
		WHERE docstatus = 1
			AND bank_account = %(bank_account)s
			AND date BETWEEN %(from_date)s AND %(to_date)s
			AND unallocated_amount > 0
		ORDER BY date ASC
		LIMIT %(limit)s
		""",
		{
			"bank_account": bank_account,
			"from_date": from_date,
			"to_date": to_date,
			"limit": AUTO_MATCH_LIMIT,
		},
		as_dict=True,
	)

	matched = []
	review_queue = []
	errors = []
	for transaction in transactions:
		actions = _detect_actions(transaction)
		primary = actions[0] if actions else {}
		if primary.get("action") != "Internal Transfer" or cint(primary.get("score")) < threshold:
			continue
		try:
			pair = _find_internal_transfer_pair(ctx, transaction)
			if not pair:
				review_queue.append({"bank_transaction": transaction.name, "reason": _("No unique opposite bank transaction found")})
				continue
			from_bank = transaction.bank_account if flt(transaction.withdrawal) > 0 else pair.bank_account
			to_bank = pair.bank_account if flt(transaction.withdrawal) > 0 else transaction.bank_account
			result = create_internal_transfer(
				bank_transaction=transaction.name,
				from_bank_account=from_bank,
				to_bank_account=to_bank,
				amount=_transaction_amount(transaction),
				date=transaction.date,
				reference_number=transaction.reference_number or transaction.transaction_id,
				mode=None,
				remarks=transaction.description,
			)
			try:
				reconcile_transaction(pair.name, [{"doctype": "Payment Entry", "name": result["name"]}])
			except Exception:
				frappe.log_error(frappe.get_traceback(), "Recon Tool Internal Transfer Pair Reconcile")
			matched.append({"bank_transaction": transaction.name, "pair": pair.name, "payment_entry": result["name"]})
		except Exception as exc:
			frappe.log_error(frappe.get_traceback(), "Recon Tool Auto Internal Transfer")
			errors.append({"bank_transaction": transaction.name, "error": str(exc)})
	return {"matched_count": len(matched), "matched": matched, "review_queue": review_queue, "errors": errors}


def _find_internal_transfer_pair(ctx, transaction):
	amount = _transaction_amount(transaction)
	opposite_amount_field = "deposit" if flt(transaction.withdrawal) > 0 else "withdrawal"
	rows = frappe.db.sql(
		f"""
		SELECT bt.name, bt.bank_account, bt.date, bt.deposit, bt.withdrawal, bt.description,
			bt.reference_number, bt.transaction_id
		FROM `tabBank Transaction` bt
		INNER JOIN `tabBank Account` ba ON ba.name = bt.bank_account
		WHERE bt.docstatus = 1
			AND bt.unallocated_amount > 0
			AND bt.bank_account != %(bank_account)s
			AND ba.company = %(company)s
			AND bt.date BETWEEN %(from_date)s AND %(to_date)s
			AND bt.{opposite_amount_field} = %(amount)s
		ORDER BY ABS(DATEDIFF(bt.date, %(date)s)) ASC, bt.creation ASC
		LIMIT 2
		""",
		{
			"bank_account": transaction.bank_account,
			"company": ctx.company,
			"from_date": add_days(transaction.date, -3),
			"to_date": add_days(transaction.date, 3),
			"date": transaction.date,
			"amount": amount,
		},
		as_dict=True,
	)
	return rows[0] if len(rows) == 1 else None


def _create_payment_entry(
	ctx,
	amount,
	date,
	party_type,
	party,
	mode,
	narration,
	reference_number,
	direction,
	counterparty_account=None,
	bank_direction="Pay",
):
	_check_perm("Payment Entry", "create")
	if direction == "Internal Transfer":
		if not counterparty_account:
			frappe.throw(_("Counterparty Account is required for Internal Transfer"))
		_validate_company_account(counterparty_account, ctx.company, _("Counterparty Account"))
		doc = frappe.new_doc("Payment Entry")
		doc.company = ctx.company
		doc.posting_date = date
		doc.payment_type = "Internal Transfer"
		doc.mode_of_payment = mode
		doc.reference_no = reference_number or "BANK-TRANSFER"
		doc.reference_date = date
		doc.remarks = narration
		doc.paid_amount = amount
		doc.received_amount = amount
		if bank_direction == "Receive":
			doc.paid_from = counterparty_account
			doc.paid_to = ctx.account
		else:
			doc.paid_from = ctx.account
			doc.paid_to = counterparty_account
		doc.insert()
		doc.submit()
		return doc.name

	if party_type not in PARTY_DOCTYPES or not party:
		frappe.throw(_("Party Type and Party are required for Payment Entry"))
	from erpnext.accounts.party import get_party_account

	party_account = get_party_account(party_type, party, ctx.company)
	doc = frappe.new_doc("Payment Entry")
	doc.company = ctx.company
	doc.posting_date = date
	doc.payment_type = direction
	doc.party_type = party_type
	doc.party = party
	doc.mode_of_payment = mode
	doc.reference_no = reference_number or "BANK-RECON"
	doc.reference_date = date
	doc.remarks = narration
	doc.bank_account = ctx.name
	doc.paid_amount = amount
	doc.received_amount = amount
	if direction == "Receive":
		doc.paid_from = party_account
		doc.paid_to = ctx.account
	else:
		doc.paid_from = ctx.account
		doc.paid_to = party_account
	doc.insert()
	doc.submit()
	return doc.name


def _create_journal_entry(ctx, amount, date, counterparty_account, narration, reference_number, direction, entry_type=None):
	_check_perm("Journal Entry", "create")
	if not counterparty_account:
		frappe.throw(_("Counterparty Account is required for Journal Entry"))
	if not frappe.db.exists("Account", {"name": counterparty_account, "company": ctx.company, "is_group": 0}):
		frappe.throw(_("Select a ledger Account for the same company"))
	doc = frappe.new_doc("Journal Entry")
	doc.company = ctx.company
	doc.voucher_type = "Contra Entry" if entry_type == "Contra Entry" else "Journal Entry"
	doc.posting_date = date
	doc.cheque_no = reference_number
	doc.cheque_date = date
	doc.user_remark = narration
	bank_is_debit = direction == "Receive"
	doc.append("accounts", {
		"account": ctx.account,
		"debit_in_account_currency": amount if bank_is_debit else 0,
		"credit_in_account_currency": 0 if bank_is_debit else amount,
	})
	doc.append("accounts", {
		"account": counterparty_account,
		"debit_in_account_currency": 0 if bank_is_debit else amount,
		"credit_in_account_currency": amount if bank_is_debit else 0,
	})
	doc.insert()
	doc.submit()
	return doc.name


def _create_invoice(voucher_type, ctx, amount, date, party, narration):
	_check_perm(voucher_type, "create")
	if not party:
		frappe.throw(_("Party is required for {0}").format(voucher_type))
	item = frappe.db.get_value("Item", {"is_stock_item": 0, "disabled": 0}, "name")
	if not item:
		frappe.throw(_("Create a non-stock Item before creating invoices from reconciliation"))
	doc = frappe.new_doc(voucher_type)
	doc.company = ctx.company
	doc.posting_date = date
	doc.due_date = date
	doc.remarks = narration
	if voucher_type == "Sales Invoice":
		doc.customer = party
	else:
		doc.supplier = party
	doc.append("items", {"item_code": item, "qty": 1, "rate": amount})
	doc.insert()
	doc.submit()
	return doc.name


@frappe.whitelist()
def split_transaction(bank_transaction, splits):
	_check_perm("Bank Transaction", "write")
	splits = _as_json(splits, []) or []
	if not splits:
		frappe.throw(_("Add at least one split row"))
	transaction = _get_transaction_doc(bank_transaction)
	total = sum(flt(row.get("amount")) for row in splits)
	if flt(total, 2) != flt(transaction.unallocated_amount, 2):
		frappe.throw(_("Split total must equal the transaction unallocated amount"))
	return {
		"status": "review",
		"bank_transaction": bank_transaction,
		"splits": splits,
		"message": _("Split rows are validated. Create vouchers for each split, then reconcile them against this Bank Transaction."),
	}
