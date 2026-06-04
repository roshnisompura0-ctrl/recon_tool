"""Compatibility wrappers for the Bank Reconciliation custom page.

The production API lives in ``recon_tool.api.bank_recon`` so the client page,
tests and integrations use a single reconciliation implementation. These
wrappers keep older dotted paths working if a saved script or browser cache
still calls the page module directly.
"""

from recon_tool.api.bank_recon import (  # noqa: F401
	auto_reconcile,
	auto_reconcile_internal_transfer,
	create_contra_entry,
	create_internal_transfer,
	create_voucher,
	create_voucher_and_reconcile,
	detect_transaction_type,
	fetch_open_invoices,
	fetch_parties,
	get_bank_transactions,
	get_reconciliation_suggestions,
	reconcile_transaction,
	split_transaction,
)
