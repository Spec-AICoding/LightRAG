import * as React from 'react'
import * as TooltipPrimitive from '@radix-ui/react-tooltip'
import { cn } from '@/lib/utils'

const TooltipProvider = TooltipPrimitive.Provider

const Tooltip = TooltipPrimitive.Root

const TooltipTrigger = TooltipPrimitive.Trigger

const processTooltipContent = (content: string) => {
  if (typeof content !== 'string') return content
  return (
    <div className="relative top-0 pt-1 whitespace-nowrap">
      {content}
    </div>
  )
}

const TooltipContent = React.forwardRef<
  React.ComponentRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content> & {
    side?: 'top' | 'right' | 'bottom' | 'left'
    align?: 'start' | 'center' | 'end'
  }
>(({ className, side = 'left', align = 'start', children, ...props }, ref) => {
  const contentRef = React.useRef<HTMLDivElement>(null);

  React.useEffect(() => {
    if (contentRef.current) {
      contentRef.current.scrollTop = 0;
    }
  }, [children]);

  return (
    // Portal: the tooltip mounts on <body> so it is never clipped by an
    // ancestor with overflow/backdrop-filter (e.g. the graph properties
    // panel's scrollable list). Single-line no-wrap content with an 80vw
    // cap and horizontal scroll keeps the FULL value visible — nothing is
    // truncated or wrapped.
    <TooltipPrimitive.Portal>
      <TooltipPrimitive.Content
        ref={ref}
        side={side}
        align={align}
        className={cn(
          'bg-popover text-popover-foreground animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 max-w-[80vw] overflow-x-auto whitespace-nowrap rounded-md border px-3 py-2 text-sm shadow-md z-60',
          className
        )}
        {...props}
      >
        {typeof children === 'string' ? processTooltipContent(children) : children}
      </TooltipPrimitive.Content>
    </TooltipPrimitive.Portal>
  );
})
TooltipContent.displayName = TooltipPrimitive.Content.displayName

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
