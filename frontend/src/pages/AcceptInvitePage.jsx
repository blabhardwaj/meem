import React, { useState } from 'react';
import { Navigate, useSearchParams } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import Input from '../components/ui/Input';
import Button from '../components/ui/Button';

const AcceptInvitePage = () => {
  const { isAuthenticated, acceptInvite } = useAuth();
  const [searchParams] = useSearchParams();
  const token = searchParams.get('token') || '';
  const [form, setForm] = useState({ full_name: '', password: '' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  if (isAuthenticated) return <Navigate to="/" replace />;

  const update = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      await acceptInvite({ token, password: form.password, full_name: form.full_name });
    } catch (err) {
      setError(err.detail?.detail || err.message || 'Could not accept this invite.');
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center p-5">
        <div className="max-w-md text-center">
          <p className="text-gray-300">This invite link is missing its token.</p>
          <p className="text-gray-500 text-sm mt-2">Ask whoever invited you to send the link again.</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-5">
      <div className="w-full max-w-md">
        <div className="text-center mb-8">
          <div className="inline-flex items-center gap-2 text-2xl font-bold text-gray-100">
            <Sparkles className="text-primary" size={26} />
            DocFlow <span className="text-primary">AI</span>
          </div>
          <p className="text-gray-400 mt-2">Accept your invite</p>
        </div>

        <div className="bg-surface border border-border rounded-xl p-6 shadow-lg shadow-black/20">
          <form onSubmit={handleSubmit} className="space-y-4">
            <Input label="Full name" value={form.full_name} onChange={update('full_name')} placeholder="Jane Doe" />
            <Input label="Password" type="password" required value={form.password} onChange={update('password')} placeholder="••••••••" />

            {error && <p className="text-sm text-red-400">{error}</p>}

            <Button type="submit" className="w-full" loading={busy}>
              Accept invite &amp; sign in
            </Button>
          </form>
        </div>
      </div>
    </div>
  );
};

export default AcceptInvitePage;
