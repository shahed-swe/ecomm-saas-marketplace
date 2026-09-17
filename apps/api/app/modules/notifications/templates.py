"""Default message templates, per key, channel and locale.

A tenant may override any of these (`notification_templates`), but every key ships with sensible
Bangla and English defaults so a brand-new store is never silent. Placeholders are plain
`{name}` fields filled from the event's data — no logic in templates, because a template that can
branch is a template that can crash at 2am.
"""

DEFAULTS: dict[tuple[str, str, str], tuple[str | None, str]] = {
    # key, channel, locale -> (subject/title, body)
    ("order.placed", "sms", "bn"): (None, "{store}: আপনার অর্ডার {number} পেয়েছি। মোট ৳{total}।"),
    ("order.placed", "sms", "en"): (None, "{store}: order {number} received. Total BDT {total}."),
    ("order.placed", "push", "bn"): ("অর্ডার নিশ্চিত", "অর্ডার {number} পেয়েছি, মোট ৳{total}।"),
    ("order.placed", "push", "en"): ("Order received", "Order {number}, total BDT {total}."),
    ("order.placed", "email", "en"): (
        "Your {store} order {number}",
        "Thanks for your order.\n\nOrder: {number}\nTotal: BDT {total}\n\nWe will let you know when it ships.",
    ),
    ("payment.paid", "sms", "bn"): (None, "{store}: {number} অর্ডারের ৳{total} পেমেন্ট পেয়েছি।"),
    ("payment.paid", "sms", "en"): (None, "{store}: payment of BDT {total} received for {number}."),
    ("payment.paid", "push", "bn"): ("পেমেন্ট পেয়েছি", "{number} অর্ডারের পেমেন্ট নিশ্চিত হয়েছে।"),
    ("payment.paid", "push", "en"): ("Payment received", "Payment for {number} is confirmed."),
    ("shipment.shipped", "sms", "bn"): (
        None,
        "{store}: {number} কুরিয়ারে দেওয়া হয়েছে ({courier})। ট্র্যাকিং {tracking}।",
    ),
    ("shipment.shipped", "sms", "en"): (
        None,
        "{store}: {number} handed to {courier}. Tracking {tracking}.",
    ),
    ("shipment.shipped", "push", "bn"): ("পার্সেল পাঠানো হয়েছে", "{number} এখন {courier}-এর কাছে।"),
    ("shipment.shipped", "push", "en"): ("On its way", "{number} is with {courier}."),
    ("shipment.delivered", "push", "bn"): ("ডেলিভারি সম্পন্ন", "{number} পৌঁছে গেছে। রিভিউ দিন!"),
    ("shipment.delivered", "push", "en"): ("Delivered", "{number} has arrived. Leave a review!"),
    ("return.updated", "push", "bn"): ("রিটার্ন আপডেট", "রিটার্ন {number}: {status}।"),
    ("return.updated", "push", "en"): ("Return update", "Return {number}: {status}."),
    ("refund.completed", "sms", "bn"): (None, "{store}: ৳{amount} রিফান্ড সম্পন্ন ({method})।"),
    ("refund.completed", "sms", "en"): (None, "{store}: BDT {amount} refunded via {method}."),
    ("cart.abandoned", "push", "bn"): (
        "কার্টে পণ্য রয়ে গেছে",
        "{items}টি পণ্য এখনও আপনার কার্টে আছে। শেষ হওয়ার আগেই অর্ডার করুন।",
    ),
    ("cart.abandoned", "push", "en"): (
        "Still in your cart",
        "{items} item(s) are waiting in your cart.",
    ),
    ("vendor.order", "push", "en"): ("New order", "{number} — pack it by {due}."),
    ("vendor.payout", "sms", "en"): (
        None,
        "{store}: payout of BDT {amount} sent ({reference}).",
    ),
    ("ticket.replied", "push", "en"): ("Support replied", "Ticket {number}: {preview}"),
    ("dispute.opened", "push", "en"): ("Dispute opened", "{number} needs your response by {due}."),
    # A campaign carries its own words; the template exists so the same pipeline handles it.
    ("campaign.push", "push", "bn"): ("{title}", "{body}"),
    ("campaign.push", "push", "en"): ("{title}", "{body}"),
}
MARKETING_KEYS = {"cart.abandoned", "campaign.push"}


def render(key: str, channel: str, locale: str, data: dict) -> tuple[str | None, str] | None:
    """Fall back locale → bn → en, then format. A missing placeholder never raises in production."""
    for candidate in (locale, "bn", "en"):
        template = DEFAULTS.get((key, channel, candidate))
        if template:
            subject, body = template
            return (
                subject.format_map(_Safe(data)) if subject else None,
                body.format_map(_Safe(data)),
            )
    return None


class _Safe(dict):
    def __missing__(self, key: str) -> str:  # pragma: no cover - defensive
        return ""
