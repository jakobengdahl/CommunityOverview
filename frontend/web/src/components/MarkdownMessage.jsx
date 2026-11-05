import ReactMarkdown, { defaultUrlTransform } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import rehypeKatex from 'rehype-katex';
import { useI18n } from '../i18n';
import 'katex/dist/katex.min.css';

/** Shared assistant and kiosk renderer; file-derived output cannot fetch images. */
function MarkdownMessage({ children }) {
  const { t } = useI18n();
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeKatex]}
      urlTransform={(url, key) => (key === 'src' ? url : defaultUrlTransform(url))}
      components={{
        img: ({ src, alt }) =>
          /^data:image\//i.test(src || '') ? (
            <img src={src} alt={alt || ''} />
          ) : (
            <span>
              {alt} {src}
            </span>
          ),
        a: ({ href, children }) => {
          let external;
          try {
            external =
              href && new URL(href, window.location.href).origin !== window.location.origin;
          } catch {
            return (
              <span>
                {children} {children === href ? null : href}
              </span>
            );
          }
          return (
            <a href={href} rel="noreferrer noopener">
              {children} {children === href ? null : href}
              {external ? ` (${t('chat.externalLink')})` : null}
            </a>
          );
        },
      }}
    >
      {children}
    </ReactMarkdown>
  );
}

export default MarkdownMessage;
