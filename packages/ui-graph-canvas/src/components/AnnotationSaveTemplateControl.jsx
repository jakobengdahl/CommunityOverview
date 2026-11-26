import { useContext } from 'react';
import { AnnotationContext } from './AnnotationContext';

export default function AnnotationSaveTemplateControl({ id, type, onClose }) {
  const { saveAsTemplate, labels } = useContext(AnnotationContext);
  if (!saveAsTemplate) return null;
  return (
    <button
      type="button"
      onClick={() => {
        saveAsTemplate(id, type);
        onClose();
      }}
    >
      {labels.saveAsTemplate}
    </button>
  );
}
