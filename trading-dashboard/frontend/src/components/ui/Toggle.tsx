interface Props {
  checked: boolean
  onChange: (v: boolean) => void
  disabled?: boolean
}

export function Toggle({ checked, onChange, disabled }: Props) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative w-10 h-6 rounded-full shrink-0 transition-colors disabled:opacity-40 ${
        checked ? 'bg-brand-600' : 'bg-surface-3'
      }`}
    >
      <span
        className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${
          checked ? 'translate-x-4' : 'translate-x-0'
        }`}
      />
    </button>
  )
}
