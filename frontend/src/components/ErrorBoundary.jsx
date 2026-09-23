import React from 'react';

// Without this, any uncaught render error anywhere in the tree (a null
// dereference, an unexpected API shape, etc.) unmounts everything and
// leaves a blank white page with no on-screen trace of what happened —
// the exact failure mode behind the accept-invite blank-page report.
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error('Unhandled render error:', error, info.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="min-h-screen bg-background flex items-center justify-center p-5">
          <div className="max-w-md text-center">
            <h1 className="text-xl font-bold text-gray-100 mb-2">Something went wrong</h1>
            <p className="text-gray-400 text-sm mb-4">
              {this.state.error.message || 'An unexpected error occurred.'}
            </p>
            <button
              type="button"
              onClick={() => window.location.assign('/')}
              className="px-4 py-2 rounded-lg bg-primary text-white text-sm font-medium"
            >
              Go home
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
