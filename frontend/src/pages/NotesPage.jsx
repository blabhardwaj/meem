import React, { useEffect, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { ArrowLeft, FilePlus, Plus, StickyNote, UploadCloud } from 'lucide-react';
import { notesApi, projectsApi } from '../lib/api';
import Card from '../components/ui/Card';
import Button from '../components/ui/Button';
import Modal from '../components/ui/Modal';
import Input from '../components/ui/Input';
import Textarea from '../components/ui/Textarea';
import Dropdown from '../components/ui/Dropdown';

const formatDate = (isoString) => {
  const date = new Date(isoString);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
};

const formatFileSize = (bytes) => {
  if (!bytes || bytes <= 0) return '0 KB';
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
};

const NotesPage = () => {
  const [notes, setNotes] = useState([]);
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [isModalOpen, setIsModalOpen] = useState(false);
  const [title, setTitle] = useState('');
  const [content, setContent] = useState('');
  const [projectId, setProjectId] = useState('');
  const [saving, setSaving] = useState(false);
  const [personalDocuments, setPersonalDocuments] = useState([]);
  const [documentModalOpen, setDocumentModalOpen] = useState(false);
  const [documentProjectId, setDocumentProjectId] = useState('');
  const [personalDocumentModalOpen, setPersonalDocumentModalOpen] = useState(false);
  const [personalFile, setPersonalFile] = useState(null);
  const [personalUploadError, setPersonalUploadError] = useState('');
  const [personalUploading, setPersonalUploading] = useState(false);
  const navigate = useNavigate();

  const load = async () => {
    setLoading(true);
    setError('');
    try {
      const [notesData, projectsData, personalDocumentsData] = await Promise.all([notesApi.list(), projectsApi.list(), notesApi.personalDocuments()]);
      setNotes(notesData);
      setProjects(projectsData);
      setPersonalDocuments(personalDocumentsData);
    } catch (err) {
      setError(err.message || 'Could not load notes.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
  }, []);

  const projectOptions = [
    { label: 'Personal (no project)', value: '' },
    ...projects.map((p) => ({ label: p.project_name, value: p.project_id })),
  ];

  const projectName = (id) => projects.find((p) => p.project_id === id)?.project_name;

  const handleCreate = async () => {
    if (!title.trim() || !content.trim()) return;
    setSaving(true);
    try {
      await notesApi.create({ title: title.trim(), content: content.trim(), project_id: projectId || null });
      setIsModalOpen(false);
      setTitle('');
      setContent('');
      setProjectId('');
      await load();
    } catch (err) {
      setError(err.message || 'Could not save the note.');
    } finally {
      setSaving(false);
    }
  };

  const handlePersonalUpload = async () => {
    if (!personalFile) return;
    if (personalFile.size > 10 * 1024 * 1024) {
      setPersonalUploadError('Files must be 10 MB or smaller.');
      return;
    }
    setPersonalUploading(true);
    setPersonalUploadError('');
    try {
      const formData = new FormData();
      formData.append('file', personalFile, personalFile.name);
      await notesApi.uploadPersonalDocument(formData);
      setPersonalFile(null);
      setPersonalDocumentModalOpen(false);
      await load();
    } catch (err) {
      setPersonalUploadError(err.message || 'Could not upload the personal document.');
    } finally {
      setPersonalUploading(false);
    }
  };

  return (
    <div className="flex-1 p-8 max-w-4xl mx-auto w-full">
      <div className="mb-6">
        <Link to="/" className="text-gray-400 hover:text-gray-200 transition-colors flex items-center gap-1 text-sm mb-4">
          <ArrowLeft size={16} />
          All Projects
        </Link>
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-2xl font-bold text-gray-100 flex items-center gap-2">
              <StickyNote className="text-primary" size={22} />
              Personal Notes
            </h1>
            <p className="text-gray-400 mt-1">Private todos and meeting notes, visible only to you.</p>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="secondary" icon={UploadCloud} onClick={() => setPersonalDocumentModalOpen(true)}>Personal document</Button>
            <Button variant="secondary" icon={FilePlus} onClick={() => setDocumentModalOpen(true)}>Add document</Button>
            <Button icon={Plus} onClick={() => setIsModalOpen(true)}>New Note</Button>
          </div>
        </div>
      </div>

      {error && <p className="text-red-400 text-sm mb-4">{error}</p>}

      {loading ? (
        <div className="flex justify-center py-16">
          <div className="w-8 h-8 border-4 border-primary/30 border-t-primary rounded-full animate-spin" />
        </div>
      ) : notes.length === 0 ? (
        <div className="text-center py-16 text-gray-500">No notes yet. Create your first one.</div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          {notes.map((note) => (
            <Card key={note.note_id} title={note.title} description={formatDate(note.updated_at)}>
              <p className="text-sm text-gray-300 whitespace-pre-wrap">{note.content}</p>
              {note.project_id && (
                <p className="text-xs text-primary-light mt-3">Linked to {projectName(note.project_id) || note.project_id}</p>
              )}
            </Card>
          ))}
        </div>
      )}

      <div className="mt-8">
        <h2 className="text-lg font-semibold text-gray-100">Personal documents</h2>
        <p className="text-sm text-gray-400 mt-1">Private files stored in your Notes space and not added to a project index.</p>
        {personalDocuments.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 mt-4">
            {personalDocuments.map((document) => (
              <Card key={document.document_id} title={document.filename} description={formatDate(document.created_at)}>
                <p className="text-xs text-gray-500">{formatFileSize(document.file_size_bytes)} · Private to you</p>
              </Card>
            ))}
          </div>
        ) : <p className="text-sm text-gray-500 mt-4">No personal documents yet.</p>}
      </div>

      <Modal
        open={isModalOpen}
        onClose={() => setIsModalOpen(false)}
        title="New Note"
        footer={
          <>
            <Button variant="ghost" onClick={() => setIsModalOpen(false)}>Cancel</Button>
            <Button onClick={handleCreate} disabled={!title.trim() || !content.trim()} loading={saving}>Save Note</Button>
          </>
        }
      >
        <div className="space-y-4">
          <Input label="Title" required value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Follow-up items" />
          <Textarea label="Content" required value={content} onChange={(e) => setContent(e.target.value)} placeholder="Write your note..." />
          <Dropdown label="Attach to project (optional)" options={projectOptions} value={projectId} onChange={setProjectId} />
        </div>
      </Modal>

      <Modal
        open={personalDocumentModalOpen}
        onClose={() => setPersonalDocumentModalOpen(false)}
        title="Add personal document"
        description="This file stays private to you and is not indexed into a project."
        footer={
          <>
            <Button variant="ghost" onClick={() => setPersonalDocumentModalOpen(false)}>Cancel</Button>
            <Button onClick={handlePersonalUpload} disabled={!personalFile} loading={personalUploading}>Upload document</Button>
          </>
        }
      >
        <label className="flex min-h-28 cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed border-border bg-background px-4 text-center hover:border-primary/50 transition-colors">
          <UploadCloud size={24} className="text-primary mb-2" />
          <span className="text-sm text-gray-300">{personalFile ? personalFile.name : 'Choose a private document'}</span>
          <span className="text-xs text-gray-500 mt-1">PDF, DOCX, DOC, PPTX, PPT, TXT, MD, JSON · 10 MB maximum</span>
          <input type="file" className="hidden" onChange={(event) => setPersonalFile(event.target.files?.[0] || null)} accept=".pdf,.docx,.doc,.pptx,.ppt,.txt,.md,.json" />
        </label>
        {personalUploadError && <p className="text-sm text-red-400 mt-3">{personalUploadError}</p>}
      </Modal>

      <Modal
        open={documentModalOpen}
        onClose={() => setDocumentModalOpen(false)}
        title="Add document"
        description="Choose a project to open its secure document intake workflow."
        footer={
          <>
            <Button variant="ghost" onClick={() => setDocumentModalOpen(false)}>Cancel</Button>
            <Button
              onClick={() => navigate(`/projects/${documentProjectId}/upload`)}
              disabled={!documentProjectId}
            >
              Continue to upload
            </Button>
          </>
        }
      >
        {projects.length > 0 ? (
          <Dropdown
            label="Project"
            options={projects.map((project) => ({ label: project.project_name, value: project.project_id }))}
            value={documentProjectId}
            onChange={setDocumentProjectId}
            placeholder="Select a project"
          />
        ) : (
          <p className="text-sm text-gray-400">Create a project before adding a document.</p>
        )}
      </Modal>
    </div>
  );
};

export default NotesPage;
