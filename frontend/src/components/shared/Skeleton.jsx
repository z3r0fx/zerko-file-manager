import { cn } from '../../lib/utils';

export default function Skeleton({ className }) {
  return (
    <div className={cn("animate-shimmer rounded-lg", className)} />
  );
}
