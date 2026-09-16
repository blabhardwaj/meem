import fs from 'fs';
import path from 'path';
import assert from 'assert';

console.log("=== Verification: Direct Edit Stage & Settings Icon Button ===");

// 1. Inspect ProjectWorkspace.jsx
const pwPath = path.resolve('frontend/src/pages/ProjectWorkspace.jsx');
const pwContent = fs.readFileSync(pwPath, 'utf8').replace(/\r\n/g, '\n');
const expectedLine = "const canManageStages = ['org_admin', 'project_admin'].includes(role) || Boolean(user?.is_org_admin);";
assert(pwContent.includes(expectedLine), `FAILED: Expected line not found in ProjectWorkspace.jsx: ${expectedLine}`);
console.log("✓ ProjectWorkspace.jsx: canManageStages correctly scoped.");

// 2. Role Evaluation Logic
function evaluateCanManageStages(role, user) {
  return ['org_admin', 'project_admin'].includes(role) || Boolean(user?.is_org_admin);
}

assert.strictEqual(evaluateCanManageStages('viewer', { is_org_admin: false }), false);
assert.strictEqual(evaluateCanManageStages('contributor', { is_org_admin: false }), false);
assert.strictEqual(evaluateCanManageStages('team_lead', { is_org_admin: false }), false);
assert.strictEqual(evaluateCanManageStages('project_admin', { is_org_admin: false }), true);
assert.strictEqual(evaluateCanManageStages('org_admin', { is_org_admin: false }), true);
console.log("✓ Role evaluation matrix verified.");

// 3. Inspect Modal.jsx
const modalPath = path.resolve('frontend/src/components/ui/Modal.jsx');
const modalContent = fs.readFileSync(modalPath, 'utf8').replace(/\r\n/g, '\n');
assert(modalContent.includes("scrollable = false"));
assert(modalContent.includes("${scrollable ? 'overflow-y-auto max-h-[75vh]' : 'overflow-visible'}"));
console.log("✓ Modal.jsx: scrollable prop verified.");

// 4. Inspect SourcePanel.jsx
const spPath = path.resolve('frontend/src/components/sources/SourcePanel.jsx');
const spContent = fs.readFileSync(spPath, 'utf8').replace(/\r\n/g, '\n');

// Direct onEdit passed, no 3-dot kebab menu
assert(spContent.includes("onEdit={canManageStages ? () => openSettings(stage) : null}"),
  "SourcePanel.jsx must pass onEdit={canManageStages ? () => openSettings(stage) : null}");
assert(!spContent.includes("items={[{ label: 'Edit stage & settings'"),
  "SourcePanel.jsx must not use 3-dot kebab menu for edit stage & settings");
console.log("✓ SourcePanel.jsx: passes direct onEdit to StageSection without 3-dot kebab menu.");

// 5. Inspect StageSection.jsx
const ssPath = path.resolve('frontend/src/components/sources/StageSection.jsx');
const ssContent = fs.readFileSync(ssPath, 'utf8').replace(/\r\n/g, '\n');

assert(ssContent.includes("Settings2"), "StageSection.jsx must import and use Settings2 icon");
assert(ssContent.includes("onEdit = null"), "StageSection.jsx must accept onEdit prop");
assert(ssContent.includes("<Settings2 size={16} />"), "StageSection.jsx must render Settings2 icon button");
console.log("✓ StageSection.jsx: renders direct Settings2 icon button that opens stage settings immediately.");

// 6. UI Visibility Walk
console.log("\n--- Full 5-Role UI Visibility Walk (Direct Icon) ---");
const rolesToTest = [
  { name: 'viewer', role: 'viewer', is_org_admin: false, expected: false },
  { name: 'contributor', role: 'contributor', is_org_admin: false, expected: false },
  { name: 'team_lead', role: 'team_lead', is_org_admin: false, expected: false },
  { name: 'project_admin', role: 'project_admin', is_org_admin: false, expected: true },
  { name: 'org_admin', role: 'org_admin', is_org_admin: false, expected: true },
];

for (const { name, role, is_org_admin, expected } of rolesToTest) {
  const canManage = evaluateCanManageStages(role, { is_org_admin });
  assert.strictEqual(canManage, expected);
  console.log(`Role [${name.padEnd(16)}]: canManageStages=${String(canManage).padEnd(5)} | ` +
    `Header [+ Stage]: ${canManage ? 'VISIBLE' : 'HIDDEN '} | ` +
    `Stage [⚙ Settings Icon]: ${canManage ? 'VISIBLE (Direct 1-Click)' : 'HIDDEN                 '}`);
}

console.log("\nALL CHECKS PASSED!");
