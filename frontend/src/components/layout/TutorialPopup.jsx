import React, { useEffect, useState } from 'react';
import { ArrowLeft, ArrowRight, CheckCircle2, Sparkles, Bell, FolderKanban, ShieldCheck, UserRound, Upload } from 'lucide-react';
import Button from '../ui/Button';
import Modal from '../ui/Modal';
import { useAuth } from '../../context/AuthContext';

const memberSteps = [
  { title: 'Welcome to your workspace', element: 'Projects', description: 'Your project cards are the starting point for documents, team work, and project progress.', icon: FolderKanban },
  { title: 'Open a project', element: 'Project cards', description: 'Select a project to view its documents and stages. Use the project search field to find work quickly.', icon: FolderKanban },
  { title: 'Add and improve documents', element: 'Studio and Upload', description: 'Use Studio to jump into a project’s Draft and Search agents or run a gap analysis. Upload project documents when you are ready to share work.', icon: Upload },
  { title: 'Stay informed', element: 'Notifications', description: 'The bell shows organization activity, assignments, and other recent updates. Your profile menu contains the role-access FAQ.', icon: Bell },
];

const adminSteps = [
  { title: 'Welcome to admin workspace', element: 'Projects', description: 'Projects gives you the overall workspace view. Open a project to inspect its documents, stages, and ownership.', icon: FolderKanban },
  { title: 'Manage people and access', element: 'Access & Governance → Users & Access', description: 'Assign project roles and teams, search organization members, open a member name to view access, and remove project access when needed.', icon: ShieldCheck },
  { title: 'Review governance', element: 'Access & Governance → Approvals and Activity', description: 'Use Approvals for pending decisions, Project Activity for project history, and Audit Log for organization events.', icon: ShieldCheck },
  { title: 'Use the AI workspace', element: 'Studio', description: 'Jump into any project’s Draft or Search agent, or run a gap analysis to see what a project is missing.', icon: Sparkles },
  { title: 'Monitor every update', element: 'Notifications', description: 'The notification badge highlights new organization activity. The profile menu also includes the role-access FAQ.', icon: Bell },
];

const TutorialPopup = ({ open, onClose }) => {
  const { user } = useAuth();
  const steps = user?.is_org_admin ? adminSteps : memberSteps;
  const [stepIndex, setStepIndex] = useState(0);
  const step = steps[stepIndex];
  const StepIcon = step.icon;
  const isLastStep = stepIndex === steps.length - 1;

  useEffect(() => {
    if (open) setStepIndex(0);
  }, [open]);

  const finish = () => {
    const roleKey = user?.is_org_admin ? 'admin' : 'member';
    window.localStorage.setItem(`docflow_tutorial_seen_${roleKey}`, 'true');
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={finish}
      title="Quick Tour"
      description={`Step ${stepIndex + 1} of ${steps.length}`}
      footer={(
        <div className="flex w-full items-center justify-between gap-3">
          <Button variant="ghost" onClick={finish}>Skip tutorial</Button>
          <div className="flex items-center gap-2">
            {stepIndex > 0 && (
              <Button variant="secondary" icon={ArrowLeft} onClick={() => setStepIndex((index) => index - 1)}>
                Back
              </Button>
            )}
            <Button icon={isLastStep ? CheckCircle2 : ArrowRight} onClick={() => (isLastStep ? finish() : setStepIndex((index) => index + 1))}>
              {isLastStep ? 'Get started' : 'Next'}
            </Button>
          </div>
        </div>
      )}
    >
      <div className="py-4 text-center">
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-primary/10 text-primary">
          <StepIcon size={28} />
        </div>
        <h3 className="text-xl font-semibold text-gray-100">{step.title}</h3>
        <div className="mt-3 inline-flex items-center rounded-full border border-primary/30 bg-primary/10 px-3 py-1 text-xs font-medium text-primary-light">
          {step.element}
        </div>
        <p className="mx-auto mt-3 max-w-md text-sm leading-6 text-gray-400">{step.description}</p>
        <div className="mt-6 flex justify-center gap-1.5">
          {steps.map((item, index) => (
            <span key={item.title} className={`h-1.5 rounded-full transition-all ${index === stepIndex ? 'w-7 bg-primary' : 'w-1.5 bg-border'}`} />
          ))}
        </div>
      </div>
    </Modal>
  );
};

export default TutorialPopup;
