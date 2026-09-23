import React, { useState } from 'react';
import { Navigate } from 'react-router-dom';
import { Sparkles } from 'lucide-react';
import { useAuth } from '../context/AuthContext';
import Input from '../components/ui/Input';
import Button from '../components/ui/Button';

const GoogleIcon = (props) => (
  <svg viewBox="0 0 48 48" width="18" height="18" {...props}>
    <path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3c-1.6 4.6-6 8-11.3 8-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6 29.5 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.7-.4-3.5z" />
    <path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.6 15.9 18.9 13 24 13c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34.5 6 29.5 4 24 4 16.3 4 9.6 8.3 6.3 14.7z" />
    <path fill="#4CAF50" d="M24 44c5.4 0 10.3-1.8 14.1-5l-6.5-5.5C29.6 35.6 26.9 36.5 24 36.5c-5.3 0-9.7-3.4-11.3-8.1l-6.6 5.1C9.5 39.6 16.2 44 24 44z" />
    <path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.1-4.1 5.5l6.5 5.5C41.3 36 44 30.5 44 24c0-1.3-.1-2.7-.4-3.5z" />
  </svg>
);

const LoginPage = () => {
  const { isAuthenticated, login, registerOrg, loginWithGoogle } = useAuth();
  const [mode, setMode] = useState('login'); // login | register_org
  const [form, setForm] = useState({ email: '', password: '', full_name: '', organization: '' });
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  // A brand-new org lands on the onboarding wizard instead of the ordinary
  // "/" redirect below — set right before registerOrg() resolves so this
  // component's own re-render (from isAuthenticated flipping true) picks
  // the right target instead of racing a separate navigate() call.
  const [justRegisteredOrg, setJustRegisteredOrg] = useState(false);

  if (isAuthenticated) return <Navigate to={justRegisteredOrg ? '/onboarding' : '/'} replace />;

  const update = (key) => (e) => setForm((f) => ({ ...f, [key]: e.target.value }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setBusy(true);
    try {
      if (mode === 'login') {
        await login(form.email, form.password);
      } else {
        await registerOrg({
          org_name: form.organization, email: form.email, password: form.password, full_name: form.full_name,
        });
        setJustRegisteredOrg(true);
      }
    } catch (err) {
      setError(err.message || 'Something went wrong.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-5 lg:p-10">
      <div className="w-full max-w-5xl grid lg:grid-cols-[1fr_430px] gap-12 items-center">
        <div className="hidden lg:block px-8">
          <div className="flex items-center gap-2 text-sm font-bold uppercase tracking-[0.2em] text-primary mb-8"><Sparkles size={17} /> DocFlow AI</div>
          <h1 className="text-6xl font-bold tracking-tight text-gray-100 leading-[1.05]">Your work,<br /><span className="text-primary">in flow.</span></h1>
          <p className="text-lg text-gray-500 mt-6 max-w-md leading-relaxed">Turn scattered project documents into decisions, drafts, and momentum your whole team can see.</p>
          <div className="flex gap-8 mt-12 text-sm text-gray-500"><span><strong className="block text-2xl text-gray-200">01</strong>Collect sources</span><span><strong className="block text-2xl text-gray-200">02</strong>Ask &amp; analyze</span><span><strong className="block text-2xl text-gray-200">03</strong>Draft forward</span></div>
        </div>
        <div>
        <div className="text-center mb-8">
          <div className="inline-flex items-center gap-2 text-2xl font-bold text-gray-100">
            <Sparkles className="text-primary" size={26} />
            DocFlow <span className="text-primary">AI</span>
          </div>
          <p className="text-gray-400 mt-2">
            {mode === 'login' ? 'Sign in to your workspace' : 'Create a new organization'}
          </p>
        </div>

        <div className="bg-surface border border-border rounded-xl p-6 shadow-lg shadow-black/20">
          <form onSubmit={handleSubmit} className="space-y-4">
            {mode === 'register_org' && (
              <>
                <Input label="Organization name" required value={form.organization} onChange={update('organization')} placeholder="Acme Inc" />
                <Input label="Your full name" value={form.full_name} onChange={update('full_name')} placeholder="Jane Doe" />
              </>
            )}
            <Input label="Email" type="email" required value={form.email} onChange={update('email')} placeholder="you@company.com" />
            <Input label="Password" type="password" required value={form.password} onChange={update('password')} placeholder="••••••••" />

            {error && <p className="text-sm text-red-400">{error}</p>}

            <Button type="submit" className="w-full" loading={busy} disabled={busy}>
              {mode === 'login' ? 'Sign in' : 'Create organization'}
            </Button>
          </form>

          {mode === 'login' && (
            <>
              <div className="flex items-center gap-3 my-5">
                <div className="h-px bg-border flex-1" />
                <span className="text-xs text-gray-500 uppercase">or</span>
                <div className="h-px bg-border flex-1" />
              </div>
              <div className="space-y-3">
                <Button
                  type="button"
                  variant="secondary"
                  className="w-full"
                  onClick={loginWithGoogle}
                  disabled={busy}
                >
                  <GoogleIcon className="mr-2" />
                  Continue with Google
                </Button>
                <Button
                  type="button"
                  variant="ghost"
                  className="w-full"
                  onClick={() => { setError(''); setMode('register_org'); }}
                  disabled={busy}
                >
                  Create an Organization
                </Button>
              </div>
            </>
          )}
        </div>

        {mode === 'register_org' && (
          <p className="text-center text-sm text-gray-500 mt-6">
            Already have an account?{' '}
            <button
              type="button"
              className="text-primary-light hover:underline"
              onClick={() => { setError(''); setMode('login'); }}
            >
              Sign in
            </button>
          </p>
        )}
        </div>
      </div>
    </div>
  );
};

export default LoginPage;
