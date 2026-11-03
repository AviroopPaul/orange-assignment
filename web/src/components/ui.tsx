import type { LucideIcon } from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import { House, X } from "lucide-react";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: "primary" | "secondary" | "ghost";
  size?: "sm" | "md";
  icon?: LucideIcon;
}

export function Button({
  variant = "secondary",
  size = "md",
  icon: Icon,
  className = "",
  children,
  ...rest
}: ButtonProps) {
  return (
    <button type="button" className={`btn btn-${variant} btn-${size} ${className}`} {...rest}>
      {Icon && <Icon size={size === "sm" ? 14 : 16} aria-hidden="true" />}
      {children}
    </button>
  );
}

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  icon: LucideIcon;
}

export function IconButton({ label, icon: Icon, className = "", ...rest }: IconButtonProps) {
  return (
    <button type="button" className={`icon-btn ${className}`} aria-label={label} title={label} {...rest}>
      <Icon size={17} aria-hidden="true" />
    </button>
  );
}

type Tone = "neutral" | "good" | "warn" | "danger" | "accent";

export function Chip({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}

export function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <House size={15} strokeWidth={2.2} />
    </span>
  );
}

export function EmptyState({ icon: Icon, children }: { icon: LucideIcon; children: ReactNode }) {
  return (
    <div className="empty-state">
      <span className="empty-state-icon" aria-hidden="true">
        <Icon size={18} />
      </span>
      <p>{children}</p>
    </div>
  );
}

interface PopoverProps {
  trigger: (props: { open: boolean; toggle: () => void }) => ReactNode;
  children: ReactNode;
}

/** Closes on an outside click or Escape. Its content mounts only while open. */
export function Popover({ trigger, children }: PopoverProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className="popover-root" ref={root}>
      {trigger({ open, toggle: () => setOpen((current) => !current) })}
      {open && <div className="popover">{children}</div>}
    </div>
  );
}

interface SidePanelProps {
  title: string;
  subtitle?: string;
  onClose: () => void;
  children: ReactNode;
}

/** A panel docked beside the chat. On a phone it covers the screen. */
export function SidePanel({ title, subtitle, onClose, children }: SidePanelProps) {
  return (
    <section className="side-panel" aria-label={title}>
      <header className="side-panel-head">
        <div>
          <h2>{title}</h2>
          {subtitle && <p>{subtitle}</p>}
        </div>
        <IconButton label="Close panel" icon={X} onClick={onClose} />
      </header>
      <div className="side-panel-body">{children}</div>
    </section>
  );
}
