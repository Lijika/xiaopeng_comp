import { Button } from "./ui/button";

export const MAIN_STEPS = [
  { n: "1", label: "单笔核验", href: "/" },
  { n: "2", label: "人工复核", href: "/controlled/s01" },
  { n: "3", label: "补材料", href: "/controlled/s02" },
  { n: "4", label: "批准", href: "/controlled/s05/react" },
] as const;

export default function StepPager({ current }: { current: string }) {
  const index = MAIN_STEPS.findIndex((step) => step.n === current);
  const prev = index > 0 ? MAIN_STEPS[index - 1] : null;
  const next = index >= 0 && index < MAIN_STEPS.length - 1 ? MAIN_STEPS[index + 1] : null;
  if (prev === null && next === null) return null;
  return (
    <nav className="step-pager" data-testid="step-pager" aria-label="步骤切换">
      {prev !== null ? (
        <Button variant="outline" asChild>
          <a href={prev.href} data-testid="step-prev">
            上一步 · {prev.label}
          </a>
        </Button>
      ) : (
        <span />
      )}
      {next !== null ? (
        <Button asChild>
          <a href={next.href} data-testid="step-next">
            下一步 · {next.label}
          </a>
        </Button>
      ) : (
        <span />
      )}
    </nav>
  );
}
