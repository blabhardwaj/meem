// TEMPORARY POLICY RESTRICTION (2026-09-17): only stage-scope confidential-
// access requests are currently permitted. Document- and team-scope request
// UI entry points are hidden behind this flag; the components, API calls,
// and backend support all remain fully intact underneath. Flip this back to
// true (and remove the matching backend gate in
// app/services/access_requests_service.py's request_confidential_access())
// to restore document/team-scope requests.
export const DOCUMENT_AND_TEAM_SCOPE_REQUESTS_ENABLED = false;
