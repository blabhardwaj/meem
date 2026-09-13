import React from 'react';
import { Routes, Route } from 'react-router-dom';
import AppLayout from '../components/layout/AppLayout';
import ProtectedRoute from './ProtectedRoute';
import LoginPage from '../pages/LoginPage';
import ProjectsPage from '../pages/ProjectsPage';
import ProjectWorkspace from '../pages/ProjectWorkspace';
import ProjectIntelligence from '../pages/ProjectIntelligence';
import UploadDocumentPage from '../pages/UploadDocumentPage';
import DraftingInterface from '../components/studio/DraftingInterface';
import AdminPage from '../pages/AdminPage';
import NotesPage from '../pages/NotesPage';
import StudioPage from '../pages/StudioPage';
import QueryAgentPage from '../pages/QueryAgentPage';
import ScannerInterface from '../components/studio/ScannerInterface';
import ComponentShowcase from '../pages/ComponentShowcase';

const AppRoutes = () => {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />

      <Route
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/" element={<ProjectsPage />} />
        <Route path="/projects/:projectId" element={<ProjectWorkspace />} />
        <Route path="/projects/:projectId/intelligence" element={<ProjectIntelligence />} />
        <Route path="/projects/:projectId/upload" element={<UploadDocumentPage />} />
        <Route path="/projects/:projectId/studio/scan" element={<ScannerInterface />} />
        <Route path="/projects/:projectId/studio/:templateId" element={<DraftingInterface />} />
        <Route path="/notes" element={<NotesPage />} />
        <Route path="/studio" element={<StudioPage />} />
        <Route path="/studio/query" element={<QueryAgentPage />} />
        <Route path="/studio/scan" element={<ScannerInterface />} />
        <Route path="/studio/draft/:templateId" element={<DraftingInterface />} />
        <Route path="/admin" element={<AdminPage />} />
      </Route>

      {/* Dev route outside layout */}
      <Route path="/dev/components" element={<ComponentShowcase />} />
    </Routes>
  );
};

export default AppRoutes;
