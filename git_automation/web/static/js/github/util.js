// github/util.js — tiny formatting helpers shared by the GitHub view modules.

/** Compact relative time ("2h", "5d", "3w") from an ISO timestamp. */
export function relTime(iso) {
  if (!iso) return '';
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return '';
  const secs = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (secs < 60) return 'now';
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d`;
  const weeks = Math.round(days / 7);
  if (weeks < 5) return `${weeks}w`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo`;
  return `${Math.round(days / 365)}y`;
}

/** Longer "Xh ago" form for detail headers/comments. */
export function relTimeAgo(iso) {
  const t = relTime(iso);
  return t && t !== 'now' ? `${t} ago` : t;
}

/** Uppercase initial for a comment avatar. */
export function initial(name) {
  const s = String(name || '').trim();
  return s ? s[0].toUpperCase() : '?';
}
