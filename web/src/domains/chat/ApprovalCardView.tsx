import { CalendarCheck, CalendarClock, CalendarX, Check, Info, Lock } from "lucide-react";

import { Button } from "../../components/ui";
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

const HEADINGS: Record<string, string> = {
  book: "Confirm this booking",
  cancel: "Cancel this booking?",
  reschedule: "Move this booking?",
};

const ICONS = { book: CalendarCheck, cancel: CalendarX, reschedule: CalendarClock };

interface ApprovalCardViewProps {
  card: ApprovalCard;
  open: boolean;
  busy: boolean;
  onAnswer: (card: ApprovalCard, choice: "confirm" | "reject") => void;
}

/** Every value here comes from the server's approval row, never from the model's reply. */
export function ApprovalCardView({ card, open, busy, onAnswer }: ApprovalCardViewProps) {
  const heading = HEADINGS[card.action] ?? HEADINGS.book;
  const Icon = ICONS[card.action as keyof typeof ICONS] ?? CalendarCheck;
  return (
    <section className={`approval ${open ? "open" : "closed"}`} aria-label={heading}>
      <header className="approval-head">
        <span className="approval-icon" aria-hidden="true">
          <Icon size={16} />
        </span>
        <h3>{heading}</h3>
      </header>
      <dl>
        <div>
          <dt>Provider</dt>
          <dd>{card.provider_name}</dd>
        </div>
        <div>
          <dt>Service</dt>
          <dd>{SERVICE_LABELS[card.service] ?? card.service}</dd>
        </div>
        {card.old_slot_label && (
          <div>
            <dt>Now</dt>
            <dd>{card.old_slot_label}</dd>
          </div>
        )}
        <div>
          <dt>{card.old_slot_label ? "Move to" : "When"}</dt>
          <dd>{card.slot_label}</dd>
        </div>
        <div>
          <dt>Price</dt>
          <dd className="approval-price">{formatPrice(card)}</dd>
        </div>
      </dl>
      {card.notes.map((note) => (
        <p key={note} className="approval-note">
          <Info size={14} aria-hidden="true" />
          {note}
        </p>
      ))}
      {open ? (
        <div className="approval-actions">
          <Button disabled={busy} onClick={() => onAnswer(card, "reject")}>
            Not now
          </Button>
          <Button
            variant="primary"
            icon={Check}
            disabled={busy}
            onClick={() => onAnswer(card, "confirm")}
          >
            Confirm
          </Button>
        </div>
      ) : (
        <p className="approval-closed">
          <Lock size={13} aria-hidden="true" />
          This card is closed.
        </p>
      )}
    </section>
  );
}
