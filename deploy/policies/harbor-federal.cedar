// Seed policy set for the Harbor Federal demo, written by hand. The policy/ module replaces it
// with rules compiled from payments_policy.pdf and approved by a person (ARCHITECTURE flow B).
// Gate rules (D49): every policy has a unique @id; a permit tagged @escalate needs a human
// approver holding @approver_role; any forbid wins; no matching permit means deny.

@id("vendor-lookup-01")
permit (principal == Agent::"payments-ops", action == Action::"data.read", resource)
when { context.data_class == "vendor" };

@id("pay-known-payee-01")
permit (principal == Agent::"payments-ops", action == Action::"value.transfer", resource)
when { !context.payee_new && context.amount_cents <= 500000 };

@id("pay-over-5k-01")
@escalate
@approver_role("payments-supervisor")
permit (principal == Agent::"payments-ops", action == Action::"value.transfer", resource)
when { context.amount_cents > 500000 };

@id("pay-new-payee-01")
@escalate
@approver_role("payments-supervisor")
permit (principal == Agent::"payments-ops", action == Action::"value.transfer", resource)
when { context.payee_new };

@id("pay-usd-only-01")
forbid (principal, action == Action::"value.transfer", resource)
when { context.currency != "USD" };

@id("pay-cap-01")
forbid (principal, action == Action::"value.transfer", resource)
when { context.amount_cents > 25000000 };

@id("email-internal-01")
permit (principal == Agent::"payments-ops", action == Action::"comm.external", resource)
when { context.external_recipients == 0 };

@id("email-external-01")
@escalate
@approver_role("payments-supervisor")
permit (principal == Agent::"payments-ops", action == Action::"comm.external", resource)
when { context.external_recipients > 0 };
