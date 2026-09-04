export function formatInr(amount) {
  if (amount === null || amount === undefined) return "—";
  return new Intl.NumberFormat("en-IN", {
    style: "currency",
    currency: "INR",
    maximumFractionDigits: 0,
  }).format(amount);
}

export function formatCompactInr(amount) {
  if (amount === null || amount === undefined) return "—";
  const formatted = new Intl.NumberFormat("en-IN", {
    notation: "compact",
    maximumFractionDigits: 1,
  }).format(amount);
  return `₹${formatted}`;
}

export function formatPct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(digits)}%`;
}

export function formatSignedPct(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function formatSignedPoints(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)} pts`;
}

export function formatHours(value) {
  if (value === null || value === undefined) return "—";
  return `${value.toFixed(1)}h`;
}

// "B1_CONGESTION" -> "B1 Congestion". Display formatting only -- the
// bucket CODE stays exactly as the API returns it (used for filtering,
// keys, etc); this never invents a label the matrix itself doesn't
// already imply via the code's own structure.
export function formatBucketLabel(bucket) {
  if (!bucket) return bucket;
  const [prefix, ...rest] = bucket.split("_");
  const words = rest.join(" ").toLowerCase().replace(/\b\w/g, (c) => c.toUpperCase());
  return `${prefix} ${words}`.trim();
}

export function formatActionLabel(action) {
  if (!action) return action;
  return action
    .toLowerCase()
    .split("_")
    .map((w) => w[0].toUpperCase() + w.slice(1))
    .join(" ");
}

export function formatDateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("en-IN", {
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
