// Restore cancelled debounces if preparation fails before it can safely finish.
export async function prepareTemplateCapture({
  prepare,
  isCurrent,
  annotationPending,
  canvasPending,
  scheduleAnnotation,
  scheduleCanvas,
}) {
  try {
    return await prepare();
  } catch (error) {
    if (isCurrent()) {
      if (annotationPending) scheduleAnnotation();
      if (canvasPending) scheduleCanvas();
    }
    throw error;
  }
}
