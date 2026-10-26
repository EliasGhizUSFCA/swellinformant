/**
 * Human labels for message delivery states (alerts and account emails).
 * Only "delivered" means the provider confirmed delivery; "accepted" means it took the
 * message, and "captured" means a development inbox kept it (nothing reached a mailbox).
 */
export type BadgeVariant = "good" | "danger" | "info" | "muted" | "warn";

const DELIVERY_STATUS: Record<string, { label: string; variant: BadgeVariant; help: string }> = {
  queued: { label: "Queued", variant: "info", help: "Waiting for the background worker." },
  sending: { label: "Sending", variant: "info", help: "The worker is handing it to the email/SMS provider." },
  accepted: { label: "Accepted", variant: "good", help: "Accepted by the provider. Delivery is not confirmed yet." },
  captured: {
    label: "Captured (dev inbox)",
    variant: "warn",
    help: "Development mode: captured locally, not delivered to a real mailbox.",
  },
  delivered: { label: "Delivered", variant: "good", help: "The provider confirmed delivery." },
  bounced: { label: "Bounced", variant: "danger", help: "The receiving server rejected it." },
  complained: { label: "Marked as spam", variant: "danger", help: "The recipient reported it as spam." },
  rejected: { label: "Rejected", variant: "danger", help: "The provider refused or dropped it." },
  failed: { label: "Failed", variant: "danger", help: "It could not be handed to the provider." },
  skipped: { label: "Withdrawn", variant: "muted", help: "Withdrawn before sending (search paused or deleted)." },
  suppressed: { label: "Not sent", variant: "warn", help: "Held back by a rule, such as an unverified email address." },
  // Rows created before delivery tracking existed.
  sent: { label: "Accepted", variant: "good", help: "Accepted by the provider." },
};

export function deliveryStatus(status: string) {
  return DELIVERY_STATUS[status] ?? { label: status, variant: "muted" as BadgeVariant, help: "" };
}
