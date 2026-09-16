import React from 'react';
import { Routes, Route } from 'react-router-dom';
import AppLayout from '../components/layout/AppLayout';
import ProtectedRoute from './ProtectedRoute';
import LoginPage from '../pages/LoginPage';
import AcceptInvitePage from '../pages/AcceptInvitePage';
import OrgOnboardingPage from '../pages/OrgOnboardingPage';
import ProfilePage from '../pages/ProfilePage';
import ProjectsPage from '../pages/ProjectsPage';
import ProjectWorkspace from '../pages/ProjectWorkspace';
import ProjectIntelligence from '../pages/ProjectIntelligence';
import UploadDocumentPage from '../pages/UploadDocumentPage';
import AdminPage from '../pages/AdminPage';
import FaqPage from '../pages/FaqPage';
import NotesPage from '../pages/NotesPage';
import StudioPage from '../pages/StudioPage';
import QueryAgentPage from '../pages/QueryAgentPage';
import ComponentShowcase from '../pages/ComponentShowcase';

const AppRoutes = () => {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/accept-invite" element={<AcceptInvitePage />} />
      <Route
        path="/onboarding"
        element={
          <ProtectedRoute>
            <OrgOnboardingPage />
          </ProtectedRoute>
        }
      />

      <Route
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<ProjectsPage />} />
        <Route path="/profile" element={<ProfilePage />} />
        <Route path="/projects/:projectId" element={<ProjectWorkspace />} />
        <Route path="/projects/:projectId/intelligence" element={<ProjectIntelligence />} />
        <Route path="/projects/:projectId/upload" element={<UploadDocumentPage />} />
        <Route path="/notes" element={<NotesPage />} />
        <Route path="/studio" element={<StudioPage />} />
        <Route path="/studio/query" element={<QueryAgentPage />} />
        <Route path="/admin" element={<AdminPage />} />
        <Route path="/faq" element={<FaqPage />} />
      </Route>

      {/* Dev route outside layout */}
      <Route path="/dev/components" element={<ComponentShowcase />} />
    </Routes>
  );
};

export default AppRoutes;
