import { Component } from 'react';
import { AnnotationContext } from './AnnotationContext';
import './AnnotationErrorBoundary.css';

/**
 * Isolates one annotation's render failure to that annotation.
 *
 * Annotations render inside the same ReactFlow tree as the graph itself, so an
 * exception thrown while drawing one of them unmounts the whole canvas — the
 * user loses their graph because a single stored annotation carried a field
 * the current code does not expect. Nothing about the annotation model makes
 * that trade worth taking: an annotation is a decoration, the graph is the
 * work.
 *
 * This exists specifically because annotation shapes are allowed to change
 * without migrating what is already stored (nobody uses the feature yet, so
 * stored annotations are incidental test data). That licence is only safe if
 * unrecognised data degrades instead of crashing, and this is what makes it
 * degrade. It is a backstop, not a substitute for the translation layer
 * refusing malformed input in the first place — both exist, deliberately.
 *
 * A caught annotation renders as a small neutral placeholder rather than
 * disappearing, so a user can see something is there, select it, and delete
 * it. Silently rendering nothing would look like the annotation had been lost.
 */
class AnnotationErrorBoundary extends Component {
  // Context rather than props, so GraphCanvas's nodeTypes map needs no
  // dependencies: ReactFlow remounts every node when that map changes
  // identity, and a map rebuilt on a label change would clear the canvas's
  // DOM on a language switch.
  static contextType = AnnotationContext;

  constructor(props) {
    super(props);
    this.state = { failed: false };
  }

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error) {
    // Reported rather than swallowed: a crash here is a real defect worth
    // finding, it just must not cost the user their canvas.
    // AnnotationContext has a default value, so `this.context` is always an
    // object; what a missing provider leaves out is the key.
    this.context.notifyRenderFailure?.(this.props.nodeId, error);
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div
        className="graph-annotation-broken"
        data-testid="annotation-broken"
        title={this.context.labels?.brokenAnnotation || 'This annotation could not be drawn'}
      >
        !
      </div>
    );
  }
}

export default AnnotationErrorBoundary;
