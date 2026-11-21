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
 * The `[start, end)` span of the whitespace-delimited word at `caret`: what a
 * plain-mode suggestion is queried with and what accepting one replaces.
 */
export function wordAtCaret(text, caret) {
  return {
    start: text.slice(0, caret).search(/\S*$/),
    end: caret + text.slice(caret).search(/\s|$/),
  };
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

  const context = useMemo(() => ({ view: 'interactive', locale: language }), [language]);
  const nodeTypes = useMemo(() => getSuggestedNodeTypes(schema, language), [schema, language]);
  const caret = selection.value === value ? selection.caret : value.length;
  // During IME composition the query stays at its pre-composition text.
  const queried = frozenInput ?? { value, caret };
  const word = wordAtCaret(queried.value, queried.caret);
  // Slash mode reads the whole draft; plain mode completes the word at the
  // caret, so a reference can be composed into a sentence. A slash-led word
  // there is ordinary text and must not open command mode.
  let suggestionInput = queried.value;
  if (parseCommandInput(queried.value).mode !== 'command') {
    const text = queried.value.slice(word.start, word.end);
    suggestionInput = text.startsWith('/') ? '' : text;
  }
  const { mode, items, status } = useCommandSuggestions(suggestionInput, context, nodeTypes);

  const keyOf = (item) => `${item.kind}:${item.id}`;
  const optionId = (item) => `${baseId}-opt-${keyOf(item).replace(/[^\w-]/g, '_')}`;
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
  // left above the box in the visual viewport instead.
  useLayoutEffect(() => {
    const vv = window.visualViewport;
    if (!open || !vv) return undefined;
    const measure = () => {
      const top = boxRef.current?.getBoundingClientRect().top;
      if (top == null) return;
      setPopupSpace(Math.max(0, Math.floor(top - vv.offsetTop - POPUP_VIEWPORT_MARGIN_PX)));
    };
    measure();
    vv.addEventListener('resize', measure);
    vv.addEventListener('scroll', measure);
    return () => {
      vv.removeEventListener('resize', measure);
      vv.removeEventListener('scroll', measure);
    };
  }, [open]);

  useEffect(() => {
    if (activeItem)
      document.getElementById(optionId(activeItem))?.scrollIntoView?.({ block: 'nearest' });
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

  // Node and type suggestions replace the word at the caret they were queried with.
  const insert = (text) => {
    const { start, end } = wordAtCaret(value, caret);
    const rest = value.slice(end);
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
    const stillOffered =
      parseCommandInput(inputRef.current?.value ?? value).mode === 'command' &&
      command === item.command &&
      command.isAvailable(context);
    try {
      if (!stillOffered) throw new Error('command no longer available');
      await command.execute(context);
    } catch {
      setFailure(t('command_box.command_failed', { label: item.label }));
      return;
    }
    onChange('');
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
          {items.map((item) => {
            const selected = item === activeItem;
            return (
              <li
                key={keyOf(item)}
                id={optionId(item)}
                role="option"
                aria-selected={selected}
                className={`command-box-option${selected ? ' active' : ''}`}
                // Keep focus in the box, so blur cannot dismiss before the tap lands.
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => accept(item)}
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
        aria-activedescendant={activeItem ? optionId(activeItem) : undefined}
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
