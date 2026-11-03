import type { ApprovalCard } from "./types";

function formatPrice(card: ApprovalCard): string {
  const amount = card.price.toLocaleString("en-IN");
  return card.currency === "INR" ? `₹${amount}` : `${amount} ${card.currency}`;
}

const SERVICE_LABELS: Record<string, string> = {
  plumber: "Plumber",
  electrician: "Electrician",
  ac_servicing: "AC servicing",
  carpenter: "Carpenter",
  house_cleaning: "House cleaning",
};

interface ApprovalCardViewProps {
  card: ApprovalCard;
  open: boolean;
  busy: boolean;
  onAnswer: (card: ApprovalCard, choice: "confirm" | "reject") => void;
}

/** Every value here comes from the server's approval row, never from the model's reply. */
export function ApprovalCardView({ card, open, busy, onAnswer }: ApprovalCardViewProps) {
  const heading = "Confirm this booking";
  return (
    <section className={`approval ${open ? "open" : "closed"}`} aria-label={heading}>
      <h3>{heading}</h3>
      <dl>
        <div>
          <dt>Provider</dt>
          <dd>{card.provider_name}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd>{SERVICE_LABELS[card.service] ?? card.service}</dd>
        </div>
        <div>
          <dt>When</dt>
          <dd>{card.slot_label}</dd>
        </div>
        <div>
          <dt>Price</dt>
          <dd className="approval-price">{formatPrice(card)}</dd>
        </div>
      </dl>
      {card.notes.map((note) => (
        <p key={note} className="approval-note">
          {note}
        </p>
      ))}
      {open ? (
        <div className="approval-actions">
          <button
            type="button"
            className="button primary"
            disabled={busy}
            onClick={() => onAnswer(card, "confirm")}
          >
            Confirm
          </button>
          <button
            type="button"
            className="button quiet"
            disabled={busy}
            onClick={() => onAnswer(card, "reject")}
          >
            Not now
          </button>
        </div>
      ) : (
        <p className="approval-closed">This card is closed.</p>
      )}
    </section>
  );
}
