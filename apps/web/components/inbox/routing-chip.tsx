import {
  ROUTING_STATUS_LABELS,
  type RoutingStatus,
  type InboxItem,
} from "@noticedesk/shared/inbox";
import { Ban, CheckCircle2, CircleAlert, ShieldAlert, Clock } from "lucide-react";

import { Badge } from "@/components/ui/badge";

interface Props {
  item: InboxItem;
}

const TONE: Record<RoutingStatus, "neutral" | "warning" | "success" | "danger"> = {
  pending: "neutral",
  routed: "success",
  client_not_found: "warning",
  new_gst_registration_detected: "warning",
  pan_gstin_mismatch: "danger",
  no_identifier_found: "warning",
  manual_assignment: "warning",
  rejected: "danger",
};

export function RoutingChip({ item }: Props) {
  const status = item.routing_status;
  const Icon =
    status === "routed"
      ? CheckCircle2
      : status === "pan_gstin_mismatch"
        ? ShieldAlert
        : status === "rejected"
          ? Ban
        : status === "pending"
          ? Clock
          : CircleAlert;
  const label =
    status === "routed" && item.matched_client_name
      ? `Routed · ${item.matched_client_name}${
          item.matched_registration_label
            ? ` · ${item.matched_registration_label}`
            : ""
        }`
      : ROUTING_STATUS_LABELS[status];
  return (
    <Badge tone={TONE[status]}>
      <Icon
        className={
          status === "pending" ? "h-3.5 w-3.5 animate-spin" : "h-3.5 w-3.5"
        }
        aria-hidden
      />
      {label}
    </Badge>
  );
}
