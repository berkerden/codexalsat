export function JsonDetails({ value, title = 'Ham rapor ayrıntıları' }: { value: unknown; title?: string }) {
  return (
    <details className="json-details">
      <summary>{title}</summary>
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  )
}
