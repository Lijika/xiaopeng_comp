/** Shared presentation helpers for the controlled workbench panels: the
 *  labelled panel section and the leaf-value rendering used by every fact
 *  table.  One shape instead of a per-panel copy. */

export function leafText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

export function Section({
  id,
  title,
  testId,
  children,
}: {
  id: string;
  title: string;
  testId: string;
  children: React.ReactNode;
}) {
  return (
    <section className="panel" data-testid={testId} aria-labelledby={id}>
      <h2 id={id}>{title}</h2>
      {children}
    </section>
  );
}
