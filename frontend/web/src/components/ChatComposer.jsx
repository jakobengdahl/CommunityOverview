import {
  useEffect,
  useId,
  useImperativeHandle,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useI18n } from '../i18n';
import useGraphStore from '../store/graphStore';
import { getSuggestedNodeTypes } from '../commands/appCommands';
import { useCommandRegistry } from '../commands/commandRegistry';
import { parseCommandInput, useCommandSuggestions } from '../commands/commandSuggestions';

const KIND_LABEL_KEYS = {
  node: 'command_box.kind_node',
  nodeType: 'command_box.kind_node_type',
  command: 'command_box.kind_command',
};

/** The text a node suggestion inserts: a reference the assistant can resolve by id. */
export function formatNodeReference(node) {
  return `[${String(node.name ?? node.id).replace(/[[\]]/g, '')}](node:${node.id})`;
}

const isComposingKey = (e) => e.nativeEvent?.isComposing || e.keyCode === 229;

// Space kept between the popup and the visual viewport's top edge, beyond the
// popup's own offset above the box.
const POPUP_VIEWPORT_MARGIN_PX = 8;

/**
 * The node reference being typed at `caret`: the nearest `@` before it, unless
 * that `@` follows a non-whitespace character (an e-mail address), and the text
 * from there to the caret, spaces included so multi-word names can be queried.
 * `null` without one. Accepting a suggestion replaces `[start, caret)`.
 */
export function mentionAtCaret(text, caret) {
  const start = text.lastIndexOf('@', caret - 1);
  if (start < 0 || /\S/.test(text.charAt(start - 1))) return null;
  return { start, query: text.slice(start + 1, caret) };
}

/**
 * ChatPanel's single chat input. Inside the interactive App, which provides the
 * command registry, it is the command box (docs/COMMAND_BOX_DESIGN.md); without
 * a registry (kiosk views) it is a plain textarea with no suggestion machinery.
 * `ref.current.submit()` is the send control's action and follows Enter's rules.
 */
function ChatComposer(props) {
  const registry = useCommandRegistry();
  return registry ? <CommandBox {...props} registry={registry} /> : <PlainComposer {...props} />;
}

function PlainComposer({ ref, value, onChange, onSend, ...textareaProps }) {
  useImperativeHandle(ref, () => ({ submit: onSend }), [onSend]);
  return (
    <textarea
      className="chat-input"
      value={value}
      onChange={(e) => onChange(e.target.value)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && !e.shiftKey && !isComposingKey(e)) {
          e.preventDefault();
          onSend();
        }
      }}
      rows={3}
      {...textareaProps}
    />
  );
}

function CommandBox({ ref, registry, value, onChange, onSend, onFocus, disabled, placeholder }) {
  const { t, language } = useI18n();
  const schema = useGraphStore((s) => s.schema);
  const federationDepth = useGraphStore((s) => s.federationDepth);
  const baseId = useId();
  const boxRef = useRef(null);
  const inputRef = useRef(null);
  const pendingCaret = useRef(null);

  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [activeKey, setActiveKey] = useState(null);
  // The caret as last reported for `value`; any other value has it at the end.
  const [selection, setSelection] = useState({ value: '', caret: 0 });
  const [frozenInput, setFrozenInput] = useState(null);
  const [failure, setFailure] = useState(null);
  const [popupSpace, setPopupSpace] = useState(null);
  const latestValue = useRef(value);
  useLayoutEffect(() => {
    latestValue.current = value;
  }, [value]);

  // A language or graph-scope change drops the selection before the recomputed
  // results render, so Enter cannot accept an item chosen under the old scope.
  const scope = `${language}|${federationDepth}`;
  const [selectionScope, setSelectionScope] = useState(scope);
  if (selectionScope !== scope) {
    setSelectionScope(scope);
    setActiveKey(null);
  }

  const context = useMemo(() => ({ view: 'interactive', locale: language }), [language]);
  const nodeTypes = useMemo(() => getSuggestedNodeTypes(schema, language), [schema, language]);
  const caret = selection.value === value ? selection.caret : value.length;
  // During IME composition the query stays at its pre-composition text.
  const queried = frozenInput ?? { value, caret };
  // Slash mode reads the whole draft; plain text suggests only for an `@`
  // reference at the caret, whose query must never open command mode.
  let suggestionInput = queried.value;
  if (parseCommandInput(queried.value).mode !== 'command') {
    const query = mentionAtCaret(queried.value, queried.caret)?.query ?? '';
    suggestionInput = query.trimStart().startsWith('/') ? '' : query;
  }
  const { mode, items, status } = useCommandSuggestions(suggestionInput, context, nodeTypes);

  const keyOf = (item) => `${item.kind}:${item.id}`;
  // By row: item ids are arbitrary strings and no sanitizing keeps them distinct.
  const optionId = (index) => `${baseId}-opt-${index}`;
  const listboxId = `${baseId}-listbox`;
  const helpId = `${baseId}-help`;

  const active = mode !== 'idle' && focused && !disabled;
  const open = active && !dismissed && items.length > 0;
  const activeIndex = open ? items.findIndex((item) => keyOf(item) === activeKey) : -1;
  const activeItem = activeIndex >= 0 ? items[activeIndex] : null;

  useLayoutEffect(() => {
    if (pendingCaret.current === null || !inputRef.current) return;
    inputRef.current.setSelectionRange(pendingCaret.current, pendingCaret.current);
    pendingCaret.current = null;
  }, [value]);

  // dvh ignores an on-screen keyboard on iOS, so bound the popup by the room
  // left above the box in the visual viewport instead, and by the nearest
  // scrolling ancestor (the mobile bottom sheet's content), which clips it.
  useLayoutEffect(() => {
    if (!open) return undefined;
    const vv = window.visualViewport;
    const measure = () => {
      const box = boxRef.current;
      if (!box) return;
      let ceiling = vv ? vv.offsetTop : 0;
      for (let el = box.parentElement; el; el = el.parentElement) {
        if (getComputedStyle(el).overflowY !== 'visible') {
          ceiling = Math.max(ceiling, el.getBoundingClientRect().top);
          break;
        }
      }
      const top = box.getBoundingClientRect().top;
      setPopupSpace(Math.max(0, Math.floor(top - ceiling - POPUP_VIEWPORT_MARGIN_PX)));
    };
    measure();
    vv?.addEventListener('resize', measure);
    vv?.addEventListener('scroll', measure);
    return () => {
      vv?.removeEventListener('resize', measure);
      vv?.removeEventListener('scroll', measure);
    };
  }, [open]);

  useEffect(() => {
    if (activeIndex >= 0)
      document.getElementById(optionId(activeIndex))?.scrollIntoView?.({ block: 'nearest' });
  }, [activeItem]); // eslint-disable-line react-hooks/exhaustive-deps

  const close = () => {
    setDismissed(true);
    setActiveKey(null);
  };

  const edit = (target) => {
    setActiveKey(null);
    setDismissed(false);
    setFailure(null);
    setSelection({ value: target.value, caret: target.selectionStart ?? target.value.length });
    onChange(target.value);
  };

  // Node and type suggestions replace the `@` reference they were queried with.
  const insert = (text) => {
    const mention = mentionAtCaret(value, caret);
    if (!mention) return;
    const { start } = mention;
    const rest = value.slice(caret);
    const next = `${value.slice(0, start)}${text}${/^\s/.test(rest) ? '' : ' '}${rest}`;
    const nextCaret = start + text.length + 1;
    pendingCaret.current = nextCaret;
    setSelection({ value: next, caret: nextCaret });
    close();
    onChange(next);
    inputRef.current?.focus();
  };

  const runCommand = async (item) => {
    close();
    const command = registry.get(item.id);
    const accepted = latestValue.current;
    const stillOffered =
      parseCommandInput(accepted).mode === 'command' &&
      command === item.command &&
      command.isAvailable(context);
    try {
      if (!stillOffered) throw new Error('command no longer available');
      await command.execute(context);
    } catch {
      setFailure(t('command_box.command_failed', { label: item.label }));
      return;
    }
    // Text typed while the handoff was pending is a new draft; keep it.
    if (latestValue.current === accepted) onChange('');
  };

  const accept = (item) => {
    if (item.kind === 'command') runCommand(item);
    else insert(item.kind === 'node' ? formatNodeReference(item.node) : item.label);
  };

  const submit = () => {
    if (activeItem) accept(activeItem);
    else {
      close();
      onSend();
    }
  };
  useImperativeHandle(ref, () => ({ submit }));

  const handleKeyDown = (e) => {
    if (isComposingKey(e)) return;
    const eligible = active && items.length > 0;
    const step = (index) => {
      e.preventDefault();
      setDismissed(false);
      setActiveKey(keyOf(items[Math.max(0, Math.min(items.length - 1, index))]));
    };
    if (e.key === 'Tab') {
      if (!e.shiftKey && open) {
        e.preventDefault();
        accept(items[0]);
      }
    } else if (e.key === 'ArrowDown' && eligible) {
      step(activeIndex < 0 ? 0 : activeIndex + 1);
    } else if (e.key === 'ArrowUp' && open) {
      step(activeIndex < 0 ? items.length - 1 : activeIndex - 1);
    } else if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    } else if (e.key === 'Escape' && open) {
      // Only the list's Escape: the hosting panel or sheet keeps the rest.
      e.preventDefault();
      e.stopPropagation();
      close();
    }
  };

  let announcement = failure || '';
  if (!failure && active && !dismissed) {
    if (status === 'loading') announcement = t('command_box.loading');
    else if (status === 'error') announcement = t('command_box.search_failed');
    else if (items.length === 0) announcement = t('command_box.no_results');
    else announcement = t('command_box.results', { count: items.length });
  }

  return (
    <div className="command-box" ref={boxRef}>
      {open && (
        <ul
          id={listboxId}
          className="command-box-popup"
          role="listbox"
          aria-label={t('command_box.suggestions')}
          style={popupSpace === null ? undefined : { maxHeight: `min(20rem, ${popupSpace}px)` }}
        >
          {items.map((item, index) => {
            const selected = item === activeItem;
            return (
              <li
                key={keyOf(item)}
                id={optionId(index)}
                role="option"
                aria-selected={selected}
                className={`command-box-option${selected ? ' active' : ''}`}
                // Keep focus in the box, so blur cannot dismiss before the tap lands.
                onMouseDown={(e) => e.preventDefault()}
                // Accepting mid-composition would rewrite text the IME still owns.
                onClick={() => frozenInput === null && accept(item)}
              >
                <span className="command-box-option-label">{item.label}</span>
                <span className="command-box-option-kind">{t(KIND_LABEL_KEYS[item.kind])}</span>
              </li>
            );
          })}
        </ul>
      )}
      <textarea
        ref={inputRef}
        className="chat-input"
        role="combobox"
        aria-label={t('command_box.label')}
        aria-describedby={helpId}
        aria-autocomplete="list"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? listboxId : undefined}
        aria-activedescendant={activeItem ? optionId(activeIndex) : undefined}
        value={value}
        onChange={(e) => edit(e.target)}
        onSelect={(e) =>
          setSelection({ value: e.target.value, caret: e.target.selectionStart ?? 0 })
        }
        onKeyDown={handleKeyDown}
        onCompositionStart={() => setFrozenInput({ value, caret })}
        onCompositionEnd={() => setFrozenInput(null)}
        onFocus={(e) => {
          setFocused(true);
          onFocus?.(e);
        }}
        onBlur={() => {
          setFocused(false);
          setActiveKey(null);
        }}
        placeholder={placeholder}
        rows={3}
        disabled={disabled}
      />
      <span id={helpId} className="command-box-visually-hidden">
        {t('command_box.help')}
      </span>
      <span className="command-box-visually-hidden" role="status" aria-live="polite">
        {announcement}
      </span>
    </div>
  );
}

export default ChatComposer;
