"use client";

import { useEffect, useState } from "react";

import {
  DraftOptionsFields,
  type DraftOptions,
} from "@/components/drafts/draft-options";
import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";

interface Props {
  open: boolean;
  currentVersion: number;
  initial: DraftOptions;
  onClose: () => void;
  onSubmit: (opts: DraftOptions) => void;
}

// "Regenerate with options": replaces the old one-click blind regenerate so
// the partner can switch tone / reply format and add delta instructions
// for the next version.
export function RegenerateDialog({ open, currentVersion, initial, onClose, onSubmit }: Props) {
  const [opts, setOpts] = useState<DraftOptions>(initial);

  // Re-seed from the current version each time the dialog opens.
  useEffect(() => {
    if (open) setOpts(initial);
  }, [open, initial]);

  return (
    <Dialog open={open} onClose={onClose} title="Regenerate draft" className="max-w-3xl">
      <p className="mb-5 font-serif text-[13.5px] italic text-slate">
        Creates v{currentVersion + 1}. v{currentVersion} stays in the version
        history, so you can compare the two afterwards.
      </p>
      <DraftOptionsFields
        value={opts}
        onChange={setOpts}
        instructionsLabel="Instructions for this version (optional)"
        instructionsPlaceholder="e.g. Highlight the bank transaction reference on page 3 of Annexure E."
      />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onClose}>
          Cancel
        </Button>
        <Button variant="gold" onClick={() => onSubmit(opts)}>
          Generate v{currentVersion + 1}
        </Button>
      </div>
    </Dialog>
  );
}
