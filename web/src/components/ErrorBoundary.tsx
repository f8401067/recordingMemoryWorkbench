import React from 'react';
import { AlertTriangle, RotateCcw } from 'lucide-react';

type Props = { children: React.ReactNode; resetKey?: string };
type State = { error: Error | null };

export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error('[页面渲染出错]', error, info);
  }

  componentDidUpdate(prev: Props) {
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  render() {
    if (this.state.error) {
      return (
        <div className="max-w-lg mx-auto px-6 py-16 text-center">
          <div className="h-14 w-14 rounded-2xl bg-red-500/10 grid place-items-center mx-auto mb-4">
            <AlertTriangle size={24} className="text-red-500" />
          </div>
          <div className="text-[15px] font-semibold">这个页面出了点问题</div>
          <p className="text-[12.5px] text-muted mt-2 leading-relaxed break-words">
            {this.state.error.message}
          </p>
          <button
            onClick={() => {
              this.setState({ error: null });
              window.location.reload();
            }}
            className="btn-primary h-9 px-4 text-[13px] mt-5"
          >
            <RotateCcw size={14} />
            重新加载
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
