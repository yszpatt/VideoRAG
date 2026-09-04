export default function ConfirmDialog({
  open,
  title,
  message,
  confirmText = "确认",
  cancelText = "取消",
  danger = true,
  busy = false,
  onConfirm,
  onCancel,
}) {
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div
        className="modal modal-sm"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
      >
        <div className="modal-head">
          <h3 className="modal-title">{title}</h3>
        </div>
        <div className="modal-body">
          <p className="modal-confirm-msg">{message}</p>
          <div className="modal-foot-row">
            <button
              type="button"
              className="btn btn-ghost"
              onClick={onCancel}
              disabled={busy}
            >
              {cancelText}
            </button>
            <button
              type="button"
              className={`btn ${danger ? "btn-danger" : "btn-primary"}`}
              onClick={onConfirm}
              disabled={busy}
            >
              {busy ? "处理中…" : confirmText}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
