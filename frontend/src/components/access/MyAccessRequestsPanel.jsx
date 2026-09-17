import React, { useEffect, useState } from 'react';
import { Clock } from 'lucide-react';
import Modal from '../ui/Modal';
import Badge from '../ui/Badge';
import { accessRequestsApi } from '../../lib/api';

// expires_at is server-authoritative; this is a presentation-only countdown
// computed at render time — never stored, never trusted as state.
function formatCountdown(expiresAtIso) {
  if (!expiresAtIso) return null;
  const diffMs = new Date(expiresAtIso).getTime() - Date.now();
  if (diffMs <= 0) return 'expired';
  const hours = Math.round(diffMs / (1000 * 60 * 60));
  if (hours < 48) return `expires in ${hours}h`;
  return `expires in ${Math.round(hours / 24)}d`;
}

const statusBadge = {
  pending: { variant: 'warning', label: 'Pending review' },
  approved: { variant: 'success', label: 'Granted' },
  denied: { variant: 'danger', label: 'Denied' },
};

const MyAccessRequestsPanel = ({ open, onClose }) => {
  const [requests, setRequests] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setLoading(true);
    accessRequestsApi.mine()
      .then((rows) => { if (!cancelled) setRequests(Array.isArray(rows) ? rows : []); })
      .catch((err) => { if (!cancelled) setError(err.message || 'Could not load your requests.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [open]);

  return (
    <Modal open={open} onClose={onClose} title="My Access Requests" description="Every confidential-access request you've made, across every scope.">
      {loading && <p className="text-sm text-gray-500">Loading…</p>}
      {error && <p className="text-sm text-red-400">{error}</p>}
      {!loading && requests.length === 0 && (
        <p className="text-sm text-gray-500">You haven't requested confidential access to anything yet.</p>
      )}
      <div className="space-y-2">
        {requests.map((r) => {
          const badge = statusBadge[r.status] || { variant: 'neutral', label: r.status };
          const isExpiredApproved = r.status === 'approved' && !r.active;
          return (
            <div key={r.request_id} className="flex items-center justify-between gap-3 rounded-lg border border-border bg-background px-3 py-2.5">
              <div className="min-w-0">
                <p className="text-sm text-gray-200 truncate">{r.target_name}</p>
                <p className="text-xs text-gray-500 capitalize">{r.scope} access · {r.team_name}</p>
              </div>
              <div className="flex items-center gap-2 shrink-0">
                {isExpiredApproved ? (
                  <Badge variant="neutral">Expired</Badge>
                ) : (
                  <Badge variant={badge.variant}>{badge.label}</Badge>
                )}
                {r.active && r.expires_at && (
                  <span className="text-xs text-gray-500 flex items-center gap-1">
                    <Clock size={11} /> {formatCountdown(r.expires_at)}
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </Modal>
  );
};

export default MyAccessRequestsPanel;
