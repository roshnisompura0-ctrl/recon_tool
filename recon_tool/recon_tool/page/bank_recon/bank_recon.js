frappe.provide("recon_tool");

frappe.pages["bank-recon"].on_page_load = function(wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Bank Reconciliation"),
		single_column: true
	});

	wrapper.recon_tool = new recon_tool.BankReconWorkspace({ wrapper, page });
	wrapper.recon_tool.init();
};

recon_tool.BankReconWorkspace = class BankReconWorkspace {
	constructor({ wrapper, page }) {
		this.wrapper = $(wrapper);
		this.page = page;
		this.party_control = null;
		this.account_control = null;
		this.contra_debit_control = null;
		this.contra_credit_control = null;
		this.loading = {};
		this.state = {
			transactions: [],
			selected: null,
			selected_rows: new Set(),
			suggestions: [],
			selected_suggestions: new Set(),
			active_panel: "suggestions",
			voucher_type: "Payment Entry",
			party_type: "Customer",
			filters: {
				company: frappe.defaults.get_user_default("Company") || "",
				bank_account: "",
				from_date: frappe.datetime.add_months(frappe.datetime.get_today(), -1),
				to_date: frappe.datetime.get_today(),
				status: "unmatched",
				search: ""
			},
			pagination: {
				start: 0,
				page_length: 50,
				total: 0
			}
		};
	}

	init() {
		this.page.set_primary_action(__("Auto Reconcile"), () => this.auto_reconcile(), "octicon octicon-check");
		this.page.add_menu_item(__("Refresh"), () => this.refresh(), "octicon octicon-sync");
		this.render_shell();
		this.cache_elements();
		this.bind_events();
		this.set_defaults();
		this.load_companies();
		this.render_empty_context();
		this.update_entry_type_options("Payment Entry");
		this.render_dynamic_controls();
	}

	render_shell() {
		this.page.main.empty().append(`
			<div class="rt-workspace">
				<section class="rt-left">
					<div class="rt-filterbar">
						<div class="rt-control rt-control-wide">
							<label>${__("Company")}</label>
							<select id="rt-company" class="input-sm"></select>
						</div>
						<div class="rt-control rt-control-wide">
							<label>${__("Bank Account")}</label>
							<select id="rt-bank-account" class="input-sm"></select>
						</div>
						<div class="rt-control">
							<label>${__("From")}</label>
							<input id="rt-from-date" type="date" class="input-sm">
						</div>
						<div class="rt-control">
							<label>${__("To")}</label>
							<input id="rt-to-date" type="date" class="input-sm">
						</div>
						<div class="rt-control">
							<label>${__("Status")}</label>
							<select id="rt-status" class="input-sm">
								<option value="unmatched">${__("Unmatched")}</option>
								<option value="matched">${__("Matched")}</option>
								<option value="all">${__("All")}</option>
							</select>
						</div>
						<div class="rt-control rt-search-control">
							<label>${__("Search")}</label>
							<input id="rt-search" type="search" class="input-sm" placeholder="${__("Narration, reference, amount")}">
						</div>
						<button class="btn btn-primary btn-sm" id="rt-fetch">${__("Fetch")}</button>
					</div>

					<div class="rt-summary">
						<div>
							<span>${__("Bank Total")}</span>
							<strong id="rt-bank-total">0.00</strong>
						</div>
						<div>
							<span>${__("Allocated")}</span>
							<strong id="rt-allocated-total">0.00</strong>
						</div>
						<div>
							<span>${__("Unallocated")}</span>
							<strong id="rt-unallocated-total">0.00</strong>
						</div>
						<div>
							<span>${__("Open Lines")}</span>
							<strong id="rt-open-count">0</strong>
						</div>
					</div>

					<div class="rt-table-card">
						<div class="rt-table-toolbar">
							<div>
								<div class="rt-title">${__("Bank Transactions")}</div>
								<div class="rt-subtitle" id="rt-count">${__("No transactions loaded")}</div>
							</div>
							<div class="rt-toolbar-actions">
								<button class="btn btn-default btn-sm" id="rt-prev">${__("Prev")}</button>
								<button class="btn btn-default btn-sm" id="rt-next">${__("Next")}</button>
								<button class="btn btn-default btn-sm" id="rt-auto-selected">${__("Auto Selected")}</button>
							</div>
						</div>
						<div id="rt-transactions" class="rt-transactions"></div>
					</div>
				</section>

				<aside class="rt-right">
					<div class="rt-context" id="rt-context"></div>
					<div class="rt-tabs">
						<button class="rt-tab active" data-panel="suggestions">${__("Suggested Matches")}</button>
						<button class="rt-tab" data-panel="voucher">${__("Create Voucher")}</button>
						<button class="rt-tab" data-panel="invoice">${__("Match Invoice")}</button>
						<button class="rt-tab" data-panel="split">${__("Split")}</button>
					</div>
					<div class="rt-panel" data-panel-body="suggestions">
						<div class="rt-panel-head">
							<div>
								<div class="rt-title">${__("Suggested Matches")}</div>
								<div class="rt-subtitle">${__("Ranked by amount, date, references and narration")}</div>
							</div>
							<input id="rt-suggestion-search" type="search" class="input-sm" placeholder="${__("Search ERP entries")}">
						</div>
						<div id="rt-suggestions" class="rt-suggestions"></div>
						<div class="rt-panel-footer">
							<div>${__("Remaining")}: <strong id="rt-diff">0.00</strong></div>
							<button class="btn btn-primary btn-sm" id="rt-reconcile">${__("Reconcile Selected")}</button>
						</div>
					</div>

					<div class="rt-panel hidden" data-panel-body="voucher">
						<div class="rt-panel-head">
							<div>
								<div class="rt-title">${__("Create Voucher")}</div>
								<div class="rt-subtitle">${__("Create and reconcile without leaving this workspace")}</div>
							</div>
						</div>
						<div class="rt-voucher-types">
							<button class="rt-voucher-type active" data-voucher="Payment Entry">${__("Payment")}</button>
							<button class="rt-voucher-type" data-voucher="Journal Entry">${__("Journal")}</button>
							<button class="rt-voucher-type" data-voucher="Expense">${__("Expense")}</button>
						</div>
						<div class="rt-form">
							<div class="rt-form-row rt-entry-type-row">
								<div class="rt-control rt-control-full">
									<label>${__("Entry Type")}</label>
									<select id="v-entry-type" class="input-sm"></select>
								</div>
							</div>
							<div class="rt-form-row">
								<div class="rt-control">
									<label>${__("Date")}</label>
									<input id="rt-voucher-date" type="date" class="input-sm">
								</div>
								<div class="rt-control">
									<label>${__("Amount")}</label>
									<input id="rt-voucher-amount" type="number" step="0.01" class="input-sm">
								</div>
							</div>
							<div class="rt-form-row">
								<div class="rt-control">
									<label>${__("Reference")}</label>
									<input id="rt-voucher-reference" type="text" class="input-sm" placeholder="${__("UTR / cheque / ref")}">
								</div>
								<div class="rt-control rt-mode-field">
									<label>${__("Mode")}</label>
									<select id="rt-voucher-mode" class="input-sm">
										<option value="NEFT">NEFT</option>
										<option value="IMPS">IMPS</option>
										<option value="RTGS">RTGS</option>
										<option value="UPI">UPI</option>
										<option value="Cheque">Cheque</option>
										<option value="Cash">Cash</option>
									</select>
								</div>
							</div>
							<div class="rt-form-row rt-transfer-row hidden">
								<div class="rt-control">
									<label>${__("From Bank Account")}</label>
									<select id="rt-transfer-from-bank" class="input-sm"></select>
								</div>
								<div class="rt-control">
									<label>${__("To Bank Account")}</label>
									<select id="rt-transfer-to-bank" class="input-sm"></select>
								</div>
							</div>
							<div class="rt-form-row rt-party-row">
								<div class="rt-control">
									<label>${__("Party Type")}</label>
									<select id="rt-party-type" class="input-sm">
										<option value="Customer">${__("Customer")}</option>
										<option value="Supplier">${__("Supplier")}</option>
										<option value="Employee">${__("Employee")}</option>
									</select>
								</div>
								<div class="rt-control">
									<label>${__("Party")}</label>
									<div id="rt-party"></div>
								</div>
							</div>
							<div class="rt-form-row rt-account-row hidden">
								<div class="rt-control rt-control-full">
									<label>${__("Counterparty Account")}</label>
									<div id="rt-counterparty-account"></div>
								</div>
							</div>
							<div class="rt-form-row rt-contra-row hidden">
								<div class="rt-control">
									<label>${__("Debit Account")}</label>
									<div id="rt-contra-debit-account"></div>
								</div>
								<div class="rt-control">
									<label>${__("Credit Account")}</label>
									<div id="rt-contra-credit-account"></div>
								</div>
							</div>
							<div class="rt-control">
								<label>${__("Narration")}</label>
								<textarea id="rt-voucher-narration" class="input-sm" rows="3"></textarea>
							</div>
							<div class="rt-panel-footer rt-panel-footer-flat">
								<button class="btn btn-primary btn-sm" id="rt-create-voucher">${__("Create & Reconcile")}</button>
							</div>
						</div>
					</div>

					<div class="rt-panel hidden" data-panel-body="invoice">
						<div class="rt-panel-head">
							<div>
								<div class="rt-title">${__("Open Invoices")}</div>
								<div class="rt-subtitle">${__("Search unpaid sales and purchase invoices")}</div>
							</div>
							<input id="rt-invoice-search" type="search" class="input-sm" placeholder="${__("Invoice, party, remarks")}">
						</div>
						<div id="rt-invoices" class="rt-suggestions"></div>
					</div>

					<div class="rt-panel hidden" data-panel-body="split">
						<div class="rt-panel-head">
							<div>
								<div class="rt-title">${__("Split Transaction")}</div>
								<div class="rt-subtitle">${__("Prepare split rows, then create vouchers for each portion")}</div>
							</div>
							<button class="btn btn-default btn-sm" id="rt-add-split">${__("Add Row")}</button>
						</div>
						<div id="rt-splits" class="rt-splits"></div>
						<div class="rt-panel-footer">
							<div>${__("Split Total")}: <strong id="rt-split-total">0.00</strong></div>
							<button class="btn btn-primary btn-sm" id="rt-validate-split">${__("Validate Split")}</button>
						</div>
					</div>
				</aside>
			</div>
		`);
	}

	cache_elements() {
		this.$company = this.wrapper.find("#rt-company");
		this.$bank_account = this.wrapper.find("#rt-bank-account");
		this.$from_date = this.wrapper.find("#rt-from-date");
		this.$to_date = this.wrapper.find("#rt-to-date");
		this.$status = this.wrapper.find("#rt-status");
		this.$search = this.wrapper.find("#rt-search");
		this.$transactions = this.wrapper.find("#rt-transactions");
		this.$context = this.wrapper.find("#rt-context");
		this.$suggestions = this.wrapper.find("#rt-suggestions");
		this.$suggestion_search = this.wrapper.find("#rt-suggestion-search");
		this.$invoice_search = this.wrapper.find("#rt-invoice-search");
		this.$invoices = this.wrapper.find("#rt-invoices");
		this.$diff = this.wrapper.find("#rt-diff");
		this.$party_type = this.wrapper.find("#rt-party-type");
		this.$party = this.wrapper.find("#rt-party");
		this.$account = this.wrapper.find("#rt-counterparty-account");
		this.$entry_type = this.wrapper.find("#v-entry-type");
		this.$voucher_date = this.wrapper.find("#rt-voucher-date");
		this.$voucher_amount = this.wrapper.find("#rt-voucher-amount");
		this.$voucher_reference = this.wrapper.find("#rt-voucher-reference");
		this.$voucher_mode = this.wrapper.find("#rt-voucher-mode");
		this.$voucher_narration = this.wrapper.find("#rt-voucher-narration");
		this.$transfer_from_bank = this.wrapper.find("#rt-transfer-from-bank");
		this.$transfer_to_bank = this.wrapper.find("#rt-transfer-to-bank");
		this.$contra_debit = this.wrapper.find("#rt-contra-debit-account");
		this.$contra_credit = this.wrapper.find("#rt-contra-credit-account");
		this.$splits = this.wrapper.find("#rt-splits");
	}

	bind_events() {
		this.wrapper.off();
		this.wrapper.on("change", "#rt-company", () => this.on_company_change());
		this.wrapper.on("change", "#rt-bank-account", () => this.on_bank_account_change());
		this.wrapper.on("change", "#rt-from-date, #rt-to-date, #rt-status", () => this.reset_and_fetch());
		this.wrapper.on("input", "#rt-search", this.debounce(() => this.reset_and_fetch(), 300));
		this.wrapper.on("click", "#rt-fetch", () => this.reset_and_fetch());
		this.wrapper.on("click", "#rt-prev", () => this.change_page(-1));
		this.wrapper.on("click", "#rt-next", () => this.change_page(1));
		this.wrapper.on("click", "#rt-auto-selected", () => this.auto_reconcile(true));
		this.wrapper.on("click", ".rt-transaction-row", (event) => this.on_transaction_click(event));
		this.wrapper.on("change", ".rt-row-check", (event) => this.on_row_check(event));
		this.wrapper.on("click", ".rt-tab", (event) => this.switch_panel($(event.currentTarget).data("panel")));
		this.wrapper.on("click", ".rt-action-suggestion", (event) => this.apply_action_suggestion($(event.currentTarget).data("action")));
		this.wrapper.on("input", "#rt-suggestion-search", this.debounce(() => this.fetch_suggestions(), 300));
		this.wrapper.on("change", ".rt-suggestion-check", (event) => this.on_suggestion_check(event));
		this.wrapper.on("click", "#rt-reconcile", () => this.reconcile_selected());
		this.wrapper.on("click", ".rt-voucher-type", (event) => this.set_voucher_type($(event.currentTarget).data("voucher")));
		this.wrapper.on("change", "#v-entry-type", () => this.on_entry_type_change());
		this.wrapper.on("change", "#rt-party-type", () => this.on_party_type_change());
		this.wrapper.on("click", "#rt-create-voucher", () => this.create_voucher());
		this.wrapper.on("input", "#rt-invoice-search", this.debounce(() => this.fetch_invoices(), 300));
		this.wrapper.on("click", "#rt-add-split", () => this.add_split_row());
		this.wrapper.on("input", ".rt-split-amount", () => this.update_split_total());
		this.wrapper.on("click", ".rt-remove-split", (event) => $(event.currentTarget).closest(".rt-split-row").remove() && this.update_split_total());
		this.wrapper.on("click", "#rt-validate-split", () => this.validate_split());
		$(document).off("keydown.bank_recon").on("keydown.bank_recon", (event) => this.on_keydown(event));
	}

	set_defaults() {
		this.$from_date.val(this.state.filters.from_date);
		this.$to_date.val(this.state.filters.to_date);
		this.$status.val(this.state.filters.status);
		this.$voucher_date.val(this.state.filters.to_date);
	}

	load_companies() {
		frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "Company",
				fields: ["name"],
				order_by: "name asc",
				limit_page_length: 200
			},
			callback: (r) => {
				const companies = r.message || [];
				this.$company.empty();
				companies.forEach((company) => this.$company.append(`<option value="${this.esc(company.name)}">${this.esc(company.name)}</option>`));
				const company = this.state.filters.company || companies[0]?.name || "";
				this.$company.val(company);
				this.state.filters.company = company;
				this.load_bank_accounts();
			}
		});
	}

	load_bank_accounts() {
		const company = this.$company.val();
		this.$bank_account.html(`<option value="">${__("Select bank account")}</option>`);
		if (!company) return;
		frappe.call({
			method: "frappe.client.get_list",
			args: {
				doctype: "Bank Account",
				filters: { company, is_company_account: 1 },
				fields: ["name", "account"],
				order_by: "name asc",
				limit_page_length: 300
			},
			callback: (r) => {
				(r.message || []).forEach((account) => this.$bank_account.append(`<option value="${this.esc(account.name)}">${this.esc(account.name)}</option>`));
				this.populate_transfer_bank_accounts(r.message || []);
				if (this.state.filters.bank_account) {
					this.$bank_account.val(this.state.filters.bank_account);
				}
			}
		});
	}

	populate_transfer_bank_accounts(accounts) {
		const options = [`<option value="">${__("Select bank account")}</option>`]
			.concat((accounts || []).map((account) => `<option value="${this.esc(account.name)}">${this.esc(account.name)}</option>`))
			.join("");
		this.$transfer_from_bank.html(options);
		this.$transfer_to_bank.html(options);
		this.set_transfer_defaults();
	}

	on_company_change() {
		this.state.filters.company = this.$company.val();
		this.state.filters.bank_account = "";
		this.load_bank_accounts();
	}

	on_bank_account_change() {
		this.state.filters.bank_account = this.$bank_account.val();
		this.reset_and_fetch();
	}

	reset_and_fetch() {
		this.sync_filters();
		this.state.pagination.start = 0;
		this.fetch_transactions();
	}

	sync_filters() {
		this.state.filters = {
			company: this.$company.val(),
			bank_account: this.$bank_account.val(),
			from_date: this.$from_date.val(),
			to_date: this.$to_date.val(),
			status: this.$status.val(),
			search: this.$search.val()
		};
	}

	refresh() {
		this.fetch_transactions();
		if (this.state.selected) {
			this.fetch_suggestions();
		}
	}

	fetch_transactions() {
		this.sync_filters();
		if (!this.state.filters.bank_account) {
			this.render_transactions_empty(__("Select a bank account to begin."));
			return;
		}
		this.$transactions.html(this.skeleton_rows(8));
		frappe.call({
			method: "recon_tool.api.bank_recon.get_bank_transactions",
			args: {
				bank_account: this.state.filters.bank_account,
				from_date: this.state.filters.from_date,
				to_date: this.state.filters.to_date,
				status: this.state.filters.status,
				search: this.state.filters.search,
				start: this.state.pagination.start,
				page_length: this.state.pagination.page_length
			},
			callback: (r) => {
				const data = r.message || {};
				this.state.transactions = data.transactions || [];
				this.state.pagination.total = data.total || 0;
				this.render_table();
				this.render_summary(data);
			}
		});
	}

	render_summary(data) {
		this.wrapper.find("#rt-bank-total").text(this.money(data.bank_balance));
		this.wrapper.find("#rt-allocated-total").text(this.money(data.allocated_total));
		this.wrapper.find("#rt-unallocated-total").text(this.money(data.unallocated_total));
		this.wrapper.find("#rt-open-count").text(data.unmatched_count || 0);
	}

	render_table() {
		const total = this.state.pagination.total;
		const start = this.state.pagination.start;
		this.wrapper.find("#rt-count").text(__("{0} transactions, showing {1}-{2}", [
			total,
			total ? start + 1 : 0,
			Math.min(start + this.state.pagination.page_length, total)
		]));
		if (!this.state.transactions.length) {
			this.render_transactions_empty(__("No bank transactions match these filters."));
			return;
		}
		const rows = this.state.transactions.map((row) => this.transaction_row(row)).join("");
		this.$transactions.html(`
			<div class="rt-table-scroll">
				<table class="rt-transaction-table">
					<colgroup>
						<col class="rt-col-check">
						<col class="rt-col-date">
						<col class="rt-col-details">
						<col class="rt-col-reference">
						<col class="rt-col-amount">
						<col class="rt-col-status">
						<col class="rt-col-action">
					</colgroup>
					<thead>
						<tr>
							<th class="rt-check-cell"></th>
							<th>${__("Date")}</th>
							<th>${__("Details")}</th>
							<th>${__("Reference")}</th>
							<th class="rt-align-right">${__("Amount")}</th>
							<th>${__("Status")}</th>
							<th>${__("Action")}</th>
						</tr>
					</thead>
					<tbody>${rows}</tbody>
				</table>
			</div>
		`);
	}

	render_transactions() {
		this.render_table();
	}

	render_transactions_empty(message) {
		this.$transactions.html(`<div class="rt-empty">${this.esc(message)}</div>`);
		this.wrapper.find("#rt-count").text(__("No transactions loaded"));
	}

	transaction_row(row) {
		const active = this.state.selected?.name === row.name ? "active" : "";
		const checked = this.state.selected_rows.has(row.name) ? "checked" : "";
		const direction = row.direction === "Receive" ? "credit" : "debit";
		const status = row.is_matched ? __("Reconciled") : __("Open");
		const action = row.is_matched ? __("View") : __("Match");
		return `
			<tr class="rt-transaction-row ${active}" data-name="${this.esc(row.name)}" tabindex="0">
				<td class="rt-check-cell">
					<input type="checkbox" class="rt-row-check" data-name="${this.esc(row.name)}" ${checked}>
				</td>
				<td class="rt-date-cell">${this.esc(row.date || "")}</td>
				<td class="rt-details-cell">
					<div class="rt-main">
						<strong title="${this.esc(row.description || row.name)}">${this.esc(row.description || row.name)}</strong>
						<span title="${this.esc(row.bank_party_name || row.transaction_type || row.name)}">${this.esc(row.bank_party_name || row.transaction_type || row.name)}</span>
					</div>
				</td>
				<td class="rt-reference-cell" title="${this.esc(row.reference_number || row.transaction_id || "-")}">${this.esc(row.reference_number || row.transaction_id || "-")}</td>
				<td class="rt-amount-cell"><span class="rt-amount ${direction}">${this.money(row.amount)}</span></td>
				<td class="rt-status-cell"><span class="rt-chip ${row.is_matched ? "rt-chip-green" : "rt-chip-amber"}">${status}</span></td>
				<td class="rt-action-cell"><button class="btn btn-xs btn-default rt-row-action" type="button">${action}</button></td>
			</tr>
		`;
	}

	on_transaction_click(event) {
		if ($(event.target).is("input")) return;
		const name = $(event.currentTarget).data("name");
		const row = this.state.transactions.find((item) => item.name === name);
		if (!row) return;
		this.state.selected = row;
		this.state.selected_suggestions.clear();
		this.highlight_selected_row();
		this.render_context();
		this.render_voucher_defaults();
		this.fetch_suggestions();
		this.fetch_invoices();
	}

	on_row_check(event) {
		event.stopPropagation();
		const name = $(event.currentTarget).data("name");
		if ($(event.currentTarget).is(":checked")) {
			this.state.selected_rows.add(name);
		} else {
			this.state.selected_rows.delete(name);
		}
	}

	render_empty_context() {
		this.$context.html(`
			<div class="rt-context-empty">
				<div class="rt-title">${__("Select a transaction")}</div>
				<p>${__("Choose a bank line to see suggested ERP matches, create vouchers, split amounts, and reconcile from one panel.")}</p>
			</div>
		`);
		this.$suggestions.html(`<div class="rt-empty">${__("Suggestions appear after selecting a transaction.")}</div>`);
		this.$invoices.html(`<div class="rt-empty">${__("Open invoices appear after selecting a transaction.")}</div>`);
	}

	render_context() {
		const row = this.state.selected;
		const actions = row.detected_actions || [];
		const action_badges = actions.map((action) => `
			<button class="rt-action-suggestion ${this.action_class(action.action)}" data-action="${this.esc(action.action)}" type="button">
				${this.esc(action.label || action.action)}
				<span>${cint(action.score)}%</span>
			</button>
		`).join("");
		this.$context.html(`
			<div class="rt-context-top">
				<div>
					<div class="rt-kicker">${row.direction === "Receive" ? __("Money In") : __("Money Out")}</div>
					<div class="rt-context-amount">${this.money(row.amount)}</div>
				</div>
				<span class="rt-chip ${row.is_matched ? "rt-chip-green" : "rt-chip-amber"}">${row.is_matched ? __("Reconciled") : __("Open")}</span>
			</div>
			<div class="rt-context-detail">
				<div><span>${__("Date")}</span><strong>${this.esc(row.date || "")}</strong></div>
				<div><span>${__("Reference")}</span><strong>${this.esc(row.reference_number || row.transaction_id || "-")}</strong></div>
			</div>
			${action_badges ? `<div class="rt-action-suggestions">${action_badges}</div>` : ""}
			<p title="${this.esc(row.description || "")}">${this.esc(row.description || __("No narration"))}</p>
		`);
	}

	fetch_suggestions() {
		if (!this.state.selected) return;
		this.$suggestions.html(this.skeleton_rows(5));
		frappe.call({
			method: "recon_tool.api.bank_recon.get_reconciliation_suggestions",
			args: {
				bank_transaction: this.state.selected.name,
				search: this.$suggestion_search.val(),
				page_length: 60
			},
			callback: (r) => {
				this.state.suggestions = r.message?.suggestions || [];
				this.render_suggestions(this.state.suggestions, this.$suggestions);
			}
		});
	}

	render_suggestions(rows, $target) {
		if (!rows.length) {
			$target.html(`<div class="rt-empty">${__("No matches found. Try search or create a voucher.")}</div>`);
			this.update_difference();
			return;
		}
		const groups = ["Payment Entry", "Sales Invoice", "Purchase Invoice", "Journal Entry", "Expense Claim"];
		const html = groups.map((doctype) => {
			const group_rows = rows.filter((row) => row.doctype === doctype);
			if (!group_rows.length) return "";
			return `
				<div class="rt-suggestion-group">
					<div class="rt-group-head"><strong>${__(doctype)}</strong><span>${group_rows.length}</span></div>
					${group_rows.map((row) => this.suggestion_row(row)).join("")}
				</div>
			`;
		}).join("");
		$target.html(html);
		this.update_difference();
	}

	suggestion_row(row) {
		const key = `${row.doctype}|${row.name}`;
		const checked = this.state.selected_suggestions.has(key) ? "checked" : "";
		const confidence = cint(row.score) >= 86 ? "high" : cint(row.score) >= 65 ? "medium" : "low";
		return `
			<label class="rt-suggestion">
				<input type="checkbox" class="rt-suggestion-check" data-key="${this.esc(key)}" ${checked}>
				<div class="rt-suggestion-main">
					<strong>${this.esc(row.name)}</strong>
					<span>${this.esc(row.party_name || row.party || row.reference || row.remarks || "")}</span>
					<small>${this.esc(row.comment || "")}</small>
				</div>
				<div class="rt-suggestion-side">
					<strong>${this.money(row.amount)}</strong>
					<span class="rt-score ${confidence}">${cint(row.score)}%</span>
				</div>
				<div class="rt-suggestion-action">
					<span class="rt-type-badge">${this.esc(row.doctype || "")}</span>
				</div>
			</label>
		`;
	}

	on_suggestion_check(event) {
		const key = $(event.currentTarget).data("key");
		if ($(event.currentTarget).is(":checked")) {
			this.state.selected_suggestions.add(key);
		} else {
			this.state.selected_suggestions.delete(key);
		}
		this.update_difference();
	}

	apply_action_suggestion(action) {
		const map = {
			"Internal Transfer": "Payment Entry",
			"Contra Entry": "Journal Entry",
			"Customer Receipt": "Payment Entry",
			"Supplier Payment": "Payment Entry"
		};
		const voucher = map[action] || "Payment Entry";
		this.switch_panel("voucher");
		this.set_voucher_type(voucher);
		if (action === "Internal Transfer") {
			this.$entry_type.val("Internal Transfer");
			this.render_dynamic_controls("Payment Entry");
		}
		if (action === "Contra Entry") {
			this.$entry_type.val("Contra Entry");
			this.render_dynamic_controls("Journal Entry");
		}
		if (action === "Supplier Payment") {
			this.$party_type.val("Supplier");
			this.render_party_control();
		}
	}

	action_class(action) {
		if (action === "Internal Transfer") return "internal";
		if (action === "Contra Entry") return "contra";
		if (action === "Customer Receipt") return "receipt";
		return "payment";
	}

	update_difference() {
		const txn_amount = flt(this.state.selected?.amount || 0);
		let selected_total = 0;
		this.state.selected_suggestions.forEach((key) => {
			const [doctype, name] = key.split("|");
			const row = this.state.suggestions.find((item) => item.doctype === doctype && item.name === name);
			if (row) selected_total += Math.abs(flt(row.amount));
		});
		this.$diff.text(this.money(Math.abs(txn_amount - selected_total)));
	}

	reconcile_selected() {
		if (!this.state.selected) {
			frappe.show_alert({ message: __("Select a transaction first."), indicator: "orange" });
			return;
		}
		const vouchers = Array.from(this.state.selected_suggestions).map((key) => {
			const [doctype, name] = key.split("|");
			return { doctype, name };
		});
		if (!vouchers.length) {
			frappe.show_alert({ message: __("Select one or more ERP entries."), indicator: "orange" });
			return;
		}
		frappe.call({
			method: "recon_tool.api.bank_recon.reconcile_transaction",
			args: {
				bank_transaction: this.state.selected.name,
				vouchers: JSON.stringify(vouchers)
			},
			freeze: true,
			freeze_message: __("Reconciling..."),
			callback: () => {
				frappe.show_alert({ message: __("Transaction reconciled."), indicator: "green" });
				this.state.selected_suggestions.clear();
				this.fetch_transactions();
			}
		});
	}

	set_voucher_type(type) {
		this.state.voucher_type = type === "Expense" ? "Journal Entry" : type;
		this.wrapper.find(".rt-voucher-type").removeClass("active");
		this.wrapper.find(`.rt-voucher-type[data-voucher="${type}"]`).addClass("active");
		this.update_entry_type_options(type);
		this.render_dynamic_controls(type);
	}

	render_dynamic_controls(tab_type) {
		const voucher_type = this.state.voucher_type;
		const entry_type = this.$entry_type.val();
		const uses_internal_transfer = voucher_type === "Payment Entry" && entry_type === "Internal Transfer";
		const uses_party = ["Payment Entry", "Sales Invoice", "Purchase Invoice"].includes(voucher_type) && !uses_internal_transfer;
		const uses_account = voucher_type === "Journal Entry" || uses_internal_transfer;
		this.wrapper.find(".rt-party-row").toggleClass("hidden", !uses_party);
		this.wrapper.find(".rt-account-row").toggleClass("hidden", !uses_account);
		this.wrapper.find(".rt-transfer-row").addClass("hidden");
		this.wrapper.find(".rt-contra-row").addClass("hidden");
		this.wrapper.find(".rt-mode-field").toggleClass("hidden", voucher_type !== "Payment Entry");
		const default_party = voucher_type === "Purchase Invoice" ? "Supplier" : "Customer";
		if (uses_party && this.$party_type.val() !== default_party && voucher_type !== "Payment Entry") {
			this.$party_type.val(default_party);
			this.state.party_type = default_party;
		}
		if (uses_party) {
			this.render_party_control();
		}
		if (uses_account) {
			this.render_account_control(tab_type === "Expense");
		}
		this.update_voucher_button_label(tab_type);
	}

	update_voucher_button_label(tab_type) {
		let label = __("Create & Reconcile");
		if (this.state.voucher_type === "Payment Entry" && this.$entry_type.val() === "Internal Transfer") {
			label = __("Create Transfer & Reconcile");
		} else if (this.state.voucher_type === "Journal Entry" && this.$entry_type.val() === "Contra Entry") {
			label = __("Create Contra & Reconcile");
		}
		this.wrapper.find("#rt-create-voucher").text(label);
	}

	update_entry_type_options(tab_type) {
		const voucher_type = tab_type === "Expense" ? "Journal Entry" : this.state.voucher_type;
		let options = [];
		if (voucher_type === "Payment Entry") {
			options = ["Receive", "Pay", "Internal Transfer"];
		} else if (voucher_type === "Journal Entry") {
			options = ["Journal Entry", "Contra Entry"];
		}
		this.$entry_type.empty();
		options.forEach((option) => {
			this.$entry_type.append(`<option value="${this.esc(option)}">${this.esc(__(option))}</option>`);
		});
		if (voucher_type === "Payment Entry" && this.state.selected?.direction === "Pay") {
			this.$entry_type.val("Pay");
		}
		if (!options.length) {
			this.wrapper.find(".rt-entry-type-row").addClass("hidden");
		} else {
			this.wrapper.find(".rt-entry-type-row").removeClass("hidden");
		}
	}

	on_entry_type_change() {
		this.render_dynamic_controls(this.wrapper.find(".rt-voucher-type.active").data("voucher"));
	}

	on_party_type_change() {
		this.state.party_type = this.$party_type.val();
		this.render_party_control();
	}

	render_party_control() {
		if (this.party_control) {
			this.party_control.$wrapper.remove();
			this.party_control = null;
		}
		const party_type = this.$party_type.val();
		this.$party.empty();
		if (!party_type) return;
		this.party_control = new frappe.ui.form.ControlLink({
			parent: this.$party.get(0),
			df: {
				fieldtype: "Link",
				fieldname: "rt_party",
				label: __("Party"),
				options: party_type,
				placeholder: __("Select {0}", [__(party_type)]),
				get_query: () => ({
					query: "recon_tool.api.bank_recon.fetch_parties",
					filters: { party_type }
				})
			},
			render_input: true
		});
	}

	render_account_control(expense_only) {
		if (this.account_control) {
			this.account_control.$wrapper.remove();
			this.account_control = null;
		}
		this.$account.empty();
		this.account_control = new frappe.ui.form.ControlLink({
			parent: this.$account.get(0),
			df: {
				fieldtype: "Link",
				fieldname: "rt_counterparty_account",
				label: __("Counterparty Account"),
				options: "Account",
				placeholder: expense_only ? __("Select expense account") : __("Select counterparty account"),
				get_query: () => ({
					filters: {
						company: this.$company.val(),
						is_group: 0
					}
				})
			},
			render_input: true
		});
	}

	render_contra_controls() {
		if (this.contra_debit_control) {
			this.contra_debit_control.$wrapper.remove();
			this.contra_debit_control = null;
		}
		if (this.contra_credit_control) {
			this.contra_credit_control.$wrapper.remove();
			this.contra_credit_control = null;
		}
		this.$contra_debit.empty();
		this.$contra_credit.empty();
		const query = () => ({
			filters: {
				company: this.$company.val(),
				is_group: 0
			}
		});
		this.contra_debit_control = new frappe.ui.form.ControlLink({
			parent: this.$contra_debit.get(0),
			df: {
				fieldtype: "Link",
				fieldname: "rt_contra_debit_account",
				label: __("Debit Account"),
				options: "Account",
				placeholder: __("Select debit account"),
				get_query: query
			},
			render_input: true
		});
		this.contra_credit_control = new frappe.ui.form.ControlLink({
			parent: this.$contra_credit.get(0),
			df: {
				fieldtype: "Link",
				fieldname: "rt_contra_credit_account",
				label: __("Credit Account"),
				options: "Account",
				placeholder: __("Select credit account"),
				get_query: query
			},
			render_input: true
		});
	}

	set_transfer_defaults() {
		const row = this.state.selected;
		const bank_account = this.$bank_account.val();
		if (!row || !bank_account || !this.$transfer_from_bank.length) return;
		if (row.direction === "Pay") {
			this.$transfer_from_bank.val(bank_account);
			if (this.$transfer_to_bank.val() === bank_account) {
				this.$transfer_to_bank.val("");
			}
		} else {
			this.$transfer_to_bank.val(bank_account);
			if (this.$transfer_from_bank.val() === bank_account) {
				this.$transfer_from_bank.val("");
			}
		}
	}

	set_contra_defaults() {
		const row = this.state.selected;
		if (!row || !this.contra_debit_control || !this.contra_credit_control) return;
		const bank_gl = row.bank_gl_account || "";
		if (!bank_gl) return;
		if (row.direction === "Receive") {
			this.contra_debit_control.set_value(bank_gl);
		} else {
			this.contra_credit_control.set_value(bank_gl);
		}
	}

	render_voucher_defaults() {
		const row = this.state.selected;
		if (!row) return;
		this.$voucher_date.val(row.date);
		this.$voucher_amount.val(row.amount);
		this.$voucher_reference.val(row.reference_number || row.transaction_id || "");
		this.$voucher_narration.val(row.description || "");
		if (row.direction === "Pay") {
			this.$party_type.val("Supplier");
		} else {
			this.$party_type.val("Customer");
		}
		this.update_entry_type_options(this.wrapper.find(".rt-voucher-type.active").data("voucher"));
		this.render_dynamic_controls();
	}

	create_voucher() {
		if (!this.state.selected) {
			frappe.show_alert({ message: __("Select a transaction first."), indicator: "orange" });
			return;
		}
		const voucher_type = this.state.voucher_type;
		const entry_type = this.$entry_type.val();
		const party = this.party_control ? this.party_control.get_value() : "";
		const account = this.account_control ? this.account_control.get_value() : "";
		const needs_party = ["Payment Entry", "Sales Invoice", "Purchase Invoice"].includes(voucher_type) && entry_type !== "Internal Transfer";
		if (needs_party && !party) {
			frappe.show_alert({ message: __("Select a party."), indicator: "orange" });
			return;
		}
		if ((voucher_type === "Journal Entry" || entry_type === "Internal Transfer") && !account) {
			frappe.show_alert({ message: __("Select a counterparty account."), indicator: "orange" });
			return;
		}
		frappe.call({
			method: "recon_tool.api.bank_recon.create_voucher",
			args: {
				voucher_type,
				entry_type,
				bank_account: this.$bank_account.val(),
				amount: this.$voucher_amount.val(),
				date: this.$voucher_date.val(),
				party_type: this.$party_type.val(),
				party,
				mode: this.$voucher_mode.val(),
				narration: this.$voucher_narration.val(),
				reference_number: this.$voucher_reference.val(),
				counterparty_account: account,
				bank_transaction: this.state.selected.name
			},
			freeze: true,
			freeze_message: __("Creating voucher..."),
			callback: (r) => {
				frappe.show_alert({ message: __("{0} created and reconciled.", [r.message?.name || __("Voucher")]), indicator: "green" });
				this.fetch_transactions();
			}
		});
	}

	create_internal_transfer() {
		const from_bank = this.$transfer_from_bank.val();
		const to_bank = this.$transfer_to_bank.val();
		if (!from_bank || !to_bank) {
			frappe.show_alert({ message: __("Select both From and To Bank Account."), indicator: "orange" });
			return;
		}
		if (from_bank === to_bank) {
			frappe.show_alert({ message: __("From and To Bank Account cannot be the same."), indicator: "orange" });
			return;
		}
		frappe.call({
			method: "recon_tool.api.bank_recon.create_internal_transfer",
			args: {
				bank_transaction: this.state.selected.name,
				from_bank_account: from_bank,
				to_bank_account: to_bank,
				amount: this.$voucher_amount.val(),
				date: this.$voucher_date.val(),
				reference_number: this.$voucher_reference.val(),
				mode: this.$voucher_mode.val(),
				remarks: this.$voucher_narration.val()
			},
			freeze: true,
			freeze_message: __("Creating internal transfer..."),
			callback: (r) => {
				frappe.show_alert({ message: __("Internal Transfer {0} created and reconciled.", [r.message?.name]), indicator: "green" });
				this.fetch_transactions();
				this.switch_panel("suggestions");
			}
		});
	}

	create_contra_entry() {
		const debit_account = this.contra_debit_control ? this.contra_debit_control.get_value() : "";
		const credit_account = this.contra_credit_control ? this.contra_credit_control.get_value() : "";
		if (!debit_account || !credit_account) {
			frappe.show_alert({ message: __("Select both Debit and Credit Account."), indicator: "orange" });
			return;
		}
		if (debit_account === credit_account) {
			frappe.show_alert({ message: __("Debit and Credit Account cannot be the same."), indicator: "orange" });
			return;
		}
		frappe.call({
			method: "recon_tool.api.bank_recon.create_contra_entry",
			args: {
				bank_transaction: this.state.selected.name,
				debit_account,
				credit_account,
				amount: this.$voucher_amount.val(),
				date: this.$voucher_date.val(),
				reference_number: this.$voucher_reference.val(),
				remark: this.$voucher_narration.val()
			},
			freeze: true,
			freeze_message: __("Creating contra entry..."),
			callback: (r) => {
				frappe.show_alert({ message: __("Contra Entry {0} created and reconciled.", [r.message?.name]), indicator: "green" });
				this.fetch_transactions();
				this.switch_panel("suggestions");
			}
		});
	}

	fetch_invoices() {
		if (!this.state.selected) return;
		this.$invoices.html(this.skeleton_rows(4));
		frappe.call({
			method: "recon_tool.api.bank_recon.fetch_open_invoices",
			args: {
				company: this.$company.val(),
				search: this.$invoice_search.val(),
				page_length: 40
			},
			callback: (r) => {
				const invoices = r.message?.invoices || [];
				const invoice_rows = invoices.map((row) => Object.assign({ score: 0, comment: __("open invoice") }, row));
				const existing = new Set(this.state.suggestions.map((row) => `${row.doctype}|${row.name}`));
				invoice_rows.forEach((row) => {
					if (!existing.has(`${row.doctype}|${row.name}`)) {
						this.state.suggestions.push(row);
					}
				});
				this.render_suggestions(invoice_rows, this.$invoices);
			}
		});
	}

	add_split_row() {
		this.$splits.append(`
			<div class="rt-split-row">
				<input class="input-sm rt-split-label" placeholder="${__("Purpose / memo")}">
				<input class="input-sm rt-split-amount" type="number" step="0.01" placeholder="${__("Amount")}">
				<button class="btn btn-default btn-sm rt-remove-split">${__("Remove")}</button>
			</div>
		`);
	}

	update_split_total() {
		let total = 0;
		this.wrapper.find(".rt-split-amount").each((_, input) => total += flt($(input).val()));
		this.wrapper.find("#rt-split-total").text(this.money(total));
	}

	validate_split() {
		if (!this.state.selected) {
			frappe.show_alert({ message: __("Select a transaction first."), indicator: "orange" });
			return;
		}
		const splits = [];
		this.wrapper.find(".rt-split-row").each((_, row) => {
			splits.push({
				memo: $(row).find(".rt-split-label").val(),
				amount: flt($(row).find(".rt-split-amount").val())
			});
		});
		frappe.call({
			method: "recon_tool.api.bank_recon.split_transaction",
			args: {
				bank_transaction: this.state.selected.name,
				splits: JSON.stringify(splits)
			},
			callback: (r) => frappe.show_alert({ message: r.message?.message || __("Split validated."), indicator: "blue" })
		});
	}

	auto_reconcile(only_selected) {
		const names = only_selected ? Array.from(this.state.selected_rows) : null;
		if (only_selected && !names.length) {
			frappe.show_alert({ message: __("Select rows first."), indicator: "orange" });
			return;
		}
		this.sync_filters();
		if (!this.state.filters.bank_account) {
			frappe.show_alert({ message: __("Select a bank account first."), indicator: "orange" });
			return;
		}
		frappe.call({
			method: "recon_tool.api.bank_recon.auto_reconcile",
			args: {
				bank_account: this.state.filters.bank_account,
				from_date: this.state.filters.from_date,
				to_date: this.state.filters.to_date,
				threshold: 86,
				transaction_names: names ? JSON.stringify(names) : null
			},
			freeze: true,
			freeze_message: __("Running auto reconciliation..."),
			callback: (r) => {
				const data = r.message || {};
				frappe.show_alert({
					message: __("{0} matched. {1} queued for review.", [data.matched_count || 0, (data.review_queue || []).length]),
					indicator: data.matched_count ? "green" : "blue"
				});
				this.fetch_transactions();
			}
		});
	}

	auto_internal_transfers() {
		this.sync_filters();
		if (!this.state.filters.bank_account) {
			frappe.show_alert({ message: __("Select a bank account first."), indicator: "orange" });
			return;
		}
		frappe.call({
			method: "recon_tool.api.bank_recon.auto_reconcile_internal_transfer",
			args: {
				bank_account: this.state.filters.bank_account,
				from_date: this.state.filters.from_date,
				to_date: this.state.filters.to_date,
				threshold: 88
			},
			freeze: true,
			freeze_message: __("Matching internal transfers..."),
			callback: (r) => {
				const data = r.message || {};
				frappe.show_alert({
					message: __("{0} internal transfers matched. {1} need review.", [
						data.matched_count || 0,
						(data.review_queue || []).length
					]),
					indicator: data.matched_count ? "green" : "blue"
				});
				this.fetch_transactions();
			}
		});
	}

	switch_panel(panel) {
		this.state.active_panel = panel;
		this.wrapper.find(".rt-tab").removeClass("active");
		this.wrapper.find(`.rt-tab[data-panel="${panel}"]`).addClass("active");
		this.wrapper.find("[data-panel-body]").addClass("hidden");
		this.wrapper.find(`[data-panel-body="${panel}"]`).removeClass("hidden");
	}

	change_page(direction) {
		const next = this.state.pagination.start + direction * this.state.pagination.page_length;
		if (next < 0 || next >= this.state.pagination.total) return;
		this.state.pagination.start = next;
		this.fetch_transactions();
	}

	on_keydown(event) {
		if ($(event.target).is("input, textarea, select")) return;
		if (event.key === "ArrowDown") {
			event.preventDefault();
			this.move_selection(1);
		} else if (event.key === "ArrowUp") {
			event.preventDefault();
			this.move_selection(-1);
		} else if (event.key.toLowerCase() === "r") {
			event.preventDefault();
			this.reconcile_selected();
		} else if (event.key.toLowerCase() === "a") {
			event.preventDefault();
			this.auto_reconcile(false);
		} else if (event.key >= "1" && event.key <= "4") {
			const panels = ["suggestions", "voucher", "invoice", "split"];
			this.switch_panel(panels[cint(event.key) - 1]);
		}
	}

	move_selection(delta) {
		if (!this.state.transactions.length) return;
		const current = this.state.selected ? this.state.transactions.findIndex((row) => row.name === this.state.selected.name) : -1;
		const next = Math.max(0, Math.min(this.state.transactions.length - 1, current + delta));
		this.state.selected = this.state.transactions[next];
		this.state.selected_suggestions.clear();
		this.render_table();
		this.render_context();
		this.render_voucher_defaults();
		this.fetch_suggestions();
	}

	highlight_selected_row() {
		this.wrapper.find(".rt-transaction-row").removeClass("active");
		if (this.state.selected?.name) {
			this.wrapper
				.find(".rt-transaction-row")
				.filter((_, row) => $(row).data("name") === this.state.selected.name)
				.addClass("active");
		}
	}

	skeleton_rows(count) {
		return Array.from({ length: count }).map(() => `<div class="rt-skeleton"></div>`).join("");
	}

	money(value) {
		return format_currency(flt(value), frappe.defaults.get_default("currency") || undefined);
	}

	esc(value) {
		return frappe.utils.escape_html(value == null ? "" : String(value));
	}

	debounce(fn, wait) {
		let timeout;
		return (...args) => {
			clearTimeout(timeout);
			timeout = setTimeout(() => fn.apply(this, args), wait);
		};
	}
};
