import { archiveExtractor } from "./archive-source";

// Local test harness only; the Worker serves this text without evaluating it.
export const extractArchive = new Function(`return (${archiveExtractor})`)() as (compressed: Uint8Array, directory: string) => Promise<string[]>;
