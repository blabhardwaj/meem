import React from 'react';
import { HelpCircle, Check } from 'lucide-react';
import Card from '../components/ui/Card';
import BackButton from '../components/ui/BackButton';

// UI_FIXES_2026-09-15.md #21: replaces the old small header popover — a full
// page so it can hold the role table, the full Role Matrix (previously only
// reachable from Access & Governance -> Role Matrix), and an audit-rule reference, plus a
// few other common questions.

const ROLE_ACCESS = [
  { role: 'Viewer', access: 'View documents your team has access to.' },
  { role: 'Contributor', access: 'View and upload documents for your team; submit drafts for review.' },
  { role: 'Team Lead', access: 'Everything a Contributor can, plus approve/reject documents and manage team membership for your team.' },
  { role: 'Project Admin', access: 'Everything across the whole project — every team, every stage — including stage configuration and reviewing/approving anywhere.' },
  { role: 'Organization Admin', access: 'Everything across the whole organization: create projects, manage users and invites, and everything a Project Admin can on every project.' },
];

const RBAC_ACTIONS = ['view', 'upload', 'edit', 'submit', 'approve', 'reject', 'manage members'];
const RBAC_MATRIX = [
  { role: 'Viewer', scope: 'Per team', allow: ['view'] },
  { role: 'Contributor', scope: 'Per team', allow: ['view', 'upload', 'edit', 'submit'] },
  {
    role: 'Team Lead', scope: 'Per team',
    allow: ['view', 'upload', 'edit', 'submit', 'approve', 'reject', 'manage members'],
  },
  { role: 'Project Admin', scope: 'Whole project', allow: 'ALL' },
  { role: 'Organization Admin', scope: 'Whole tenant', allow: 'ALL' },
];

// Kept in sync with app/services/graph/audit_rules.py's RULE_REGISTRY.
// The original R006 ("orphan entity") was deleted (Master Plan v2 item 13)
// — every condition it checked was structurally unreachable given the
// schema. UI_FIXES_2026-09-15.md #32 renumbered R007-R012 down to R006-R011
// to close the gap that left, so R006 below is a different, unrelated rule
// (Cross-Stage Reference Violation) from the deleted one.
const AUDIT_RULES = [
  {
    code: 'R001', title: 'Missing Mandatory Requirement Evidence', severity: 'Critical',
    meaning: 'A stage requires a specific kind of document (e.g. a sign-off, a test plan) and nothing approved has been provided to satisfy it yet.',
  },
  {
    code: 'R002', title: 'Unapproved Document in Gate Stage', severity: 'High',
    meaning: "A stage that requires approval has a document sitting in draft or rejected status — it hasn't been approved, so the stage can't be considered complete.",
  },
  {
    code: 'R003', title: 'Broken Upstream Dependency', severity: 'High',
    meaning: 'A document depends on another (earlier) document, but that earlier document is unapproved or was rejected — the dependency is broken.',
  },
  {
    code: 'R004', title: 'Stale Document Reference', severity: 'Low',
    meaning: "A document references an older version of another document, and a newer approved version now exists. Currently dormant in most projects — nothing yet populates the version-pinning data this rule needs to fire.",
  },
  {
    code: 'R005', title: 'Cyclic Document Dependency', severity: 'Critical',
    meaning: 'Two or more documents depend on each other in a loop (A depends on B, B depends on A) — this can never be fully resolved as-is.',
  },
  {
    code: 'R006', title: 'Cross-Stage Reference Violation', severity: 'High',
    meaning: "A document references another stage's content in a way that isn't on that stage's permitted-references list (configurable per stage in Edit Stages).",
  },
  {
    code: 'R007', title: 'Unassigned Stage Requirement', severity: 'Medium',
    meaning: 'A stage has a mandatory requirement defined, but no document has been linked to it at all yet — not even a draft.',
  },
  {
    code: 'R008', title: 'Contradictory Statements Across Documents', severity: 'High',
    meaning: 'Two documents make claims that directly contradict each other (e.g. different numbers for the same target) — caught both by exact matching and by an AI semantic pass for contradictions phrased differently.',
  },
  {
    code: 'R009', title: 'Pending Workflow Blocker', severity: 'High',
    meaning: 'A document in a gate stage is sitting in pending review — it needs a decision (approve or reject) before the stage can be considered complete.',
  },
  {
    code: 'R010', title: 'Document-Level Coherence', severity: "High or Medium",
    meaning: "One document's own content doesn't make sense against the rest of the project's related context — distinct from R008, which only compares explicit claims across documents.",
  },
  {
    code: 'R011', title: 'Scanner-Flagged Current Version', severity: 'High',
    meaning: "A document's current version failed the Structure Scanner or was flagged by the injection check — separate from whether it's been human-approved. Matches the status shown in that document's Versions panel.",
  },
];

const SEVERITY_STYLE = {
  Critical: 'text-red-400',
  High: 'text-amber-400',
  Medium: 'text-yellow-300',
  Low: 'text-gray-400',
};

const OTHER_QUESTIONS = [
  {
    q: 'Why can\'t I see a document I know exists?',
    a: "Documents above your sensitivity clearance (Confidential) are hidden until a team lead grants you access. Use the \"Confidential access\" button in a project's workspace to request it, or ask the Search agent — it will offer to request access for you when it finds a blocked match.",
  },
  {
    q: 'What does "gated" mean on a project\'s Intelligence page?',
    a: 'A stage is gated when it requires approval and has at least one unresolved blocker (a Critical/High finding, a pending review, or missing mandatory evidence). The project can\'t be considered launch-ready until every gated stage clears.',
  },
  {
    q: 'Who can invite new people to the organization?',
    a: "Organization Admins can invite anyone, with or without a specific project/team assignment. Project Admins can add an existing account to any team in their project. Team Leads can only change the role of someone already in the project — from Access & Governance → Users & Access → Assign Roles.",
  },
  {
    q: 'What happens when I upload a new version of a document?',
    a: "It's automatically scanned (structure score, and an injection check) before it's indexed. If it was previously approved, its status resets to draft so it goes through review again — an approved document can't be silently replaced.",
  },
];

const FaqPage = () => (
  <div className="flex-1 px-6 py-8 max-w-4xl mx-auto w-full">
    <BackButton fallbackTo="/" className="mb-6" />

    <div className="mb-8 flex items-center gap-2 text-primary text-xs font-bold uppercase tracking-widest">
      <HelpCircle size={15} /> FAQ
    </div>
    <h1 className="text-3xl font-bold text-gray-100 mb-8">Frequently asked questions</h1>

    <Card title="What each role can do" description="A quick summary of access by role.">
      <div className="overflow-x-auto">
        <table className="w-full text-left text-sm">
          <thead className="text-gray-500 border-b border-border">
            <tr>
              <th className="py-2 pr-4 font-medium">Role</th>
              <th className="py-2 font-medium">Access</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border/40 text-gray-300">
            {ROLE_ACCESS.map((r) => (
              <tr key={r.role}>
                <td className="py-2.5 pr-4 font-medium text-gray-100 whitespace-nowrap">{r.role}</td>
                <td className="py-2.5 text-gray-400">{r.access}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>

    <div className="h-6" />

    <Card
      title="Role Matrix"
      description="What each role can do, action by action. Team roles are cumulative (Team Lead includes Contributor includes Viewer); Project Admin and Organization Admin bypass the per-team checks entirely."
    >
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-gray-500 border-b border-border">
              <th className="py-2 pr-4 font-medium">Role</th>
              <th className="py-2 pr-4 font-medium">Scope</th>
              {RBAC_ACTIONS.map((a) => (
                <th key={a} className="py-2 px-2 font-medium text-center whitespace-nowrap">{a}</th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border/50">
            {RBAC_MATRIX.map((row) => (
              <tr key={row.role} className="text-gray-300">
                <td className="py-2.5 pr-4 font-medium text-gray-100 whitespace-nowrap">{row.role}</td>
                <td className="py-2.5 pr-4 text-gray-500 whitespace-nowrap">{row.scope}</td>
                {RBAC_ACTIONS.map((a) => {
                  const allowed = row.allow === 'ALL' || row.allow.includes(a);
                  return (
                    <td key={a} className="py-2.5 px-2 text-center">
                      {allowed
                        ? <Check size={15} className="inline text-emerald-400" />
                        : <span className="text-gray-700">–</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-5 space-y-1.5 text-xs text-gray-500">
        <p><span className="text-gray-400">Sensitivity clearance:</span> Viewers and Contributors see Public and Internal documents; Confidential requires Team Lead+ (automatic) or a Contributor with an active access grant.</p>
        <p><span className="text-gray-400">approve / reject:</span> require Team Lead+ on the specific team a document was uploaded as.</p>
      </div>
    </Card>

    <div className="h-6" />

    <Card
      title="What each audit rule means"
      description="The rules a project audit checks, shown on a project's Intelligence page under 'What needs attention'."
    >
      <div className="divide-y divide-border/40">
        {AUDIT_RULES.map((r) => (
          <div key={r.code} className="py-3 first:pt-0 last:pb-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-xs font-mono px-1.5 py-0.5 rounded bg-primary/10 text-primary-light">{r.code}</span>
              <span className="text-sm font-medium text-gray-100">{r.title}</span>
              <span className={`text-xs font-semibold ${SEVERITY_STYLE[r.severity] || 'text-gray-400'}`}>{r.severity}</span>
            </div>
            <p className="text-sm text-gray-400 mt-1">{r.meaning}</p>
          </div>
        ))}
      </div>
    </Card>

    <div className="h-6" />

    <Card title="Other common questions">
      <div className="divide-y divide-border/40">
        {OTHER_QUESTIONS.map((item) => (
          <div key={item.q} className="py-3 first:pt-0 last:pb-0">
            <p className="text-sm font-medium text-gray-100">{item.q}</p>
            <p className="text-sm text-gray-400 mt-1">{item.a}</p>
          </div>
        ))}
      </div>
    </Card>
  </div>
);

export default FaqPage;
