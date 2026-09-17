# Taking a tenant live, and the go/no-go

## Checklist before a tenant's first real order

**Store**
- [ ] Custom domain verified and active; TLS issued (Caddy on-demand)
- [ ] Theme published; contrast guard passed; logo and favicon set
- [ ] Legal pages published (returns, privacy, contact) — required by both app stores too
- [ ] Shipping rates set for every zone they deliver to; COD rules decided

**Money**
- [ ] bKash and/or SSLCommerz credentials saved and health-checked green
- [ ] VAT settings and BIN entered; a test order produces a correct tax invoice PDF
- [ ] Commission, reserve rule and TDS rate agreed and configured
- [ ] Payout schedule set; at least one vendor has a verified payout method
- [ ] A test refund completed end to end on the real gateway (sandbox proves nothing about live keys)

**Fulfilment**
- [ ] Courier account(s) configured and health-checked; pickup address correct
- [ ] Courier rules cover every district they sell to, with a fallback rule last
- [ ] One real parcel booked, tracked and delivered

**Operations**
- [ ] Staff invited with the right roles (finance separate from support — maker-checker needs two)
- [ ] Notification templates reviewed in Bangla **and** English
- [ ] Support inbox monitored; first-response expectation agreed
- [ ] Analytics destinations connected if they want them

**Apps (if in scope)**
- [ ] Store profile complete; a build has been uploaded to internal track and installed on a device
- [ ] Force-update policy set with a sensible minimum version
- [ ] Account-deletion route tested on the device (both stores check this)

## Go / no-go

Go when: a real order placed on the live site can be paid, shipped, tracked, refunded and paid out,
and the trial balance is still balanced afterwards. That single sentence is the whole bar.

No-go, whatever the calendar says, if any of these is true:
- payments settle but the ledger drifts;
- a courier cannot be booked for a district the tenant advertises;
- staff cannot see an order they are asked about by a customer;
- the isolation suite or the security audit is failing on the deployed commit.

## First week

Watch daily: reconciliation drift, the ops queue (`needs_attention`), failed notifications, and the
support first-response time. Every one of those is an early warning that something agreed in this
checklist did not survive contact with reality.
