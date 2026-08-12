import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import 'katex/dist/katex.min.css';

/**
 * Shared rich-text renderer for AI-assistant message content.
 *
 * Single source of truth so the main graph assistant (ChatPanel) and the
 * Active Data Collection kiosk (CollectKioskView) render — and sanitize —
 * identically, rather than drifting into two parallel paths.
 *
 * Safety: react-markdown does not render raw HTML unless rehype-raw is enabled
 * (it is not), so assistant-authored HTML is escaped rather than injected. Every
 * caller of this component inherits that same safe-by-default guarantee.
 */
function MarkdownMessage({ children }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm, remarkMath]} rehypePlugins={[rehypeKatex]}>
      {children}
    </ReactMarkdown>
  );
}

export default MarkdownMessage;
