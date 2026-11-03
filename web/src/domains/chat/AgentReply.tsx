import Markdown from "react-markdown";
import remarkBreaks from "remark-breaks";
import remarkGfm from "remark-gfm";

// The model has read provider reviews, so its text never gets to link out or load an image.
const ALLOWED = [
  "p", "br", "strong", "em", "del", "ul", "ol", "li", "code", "pre", "blockquote", "hr",
  "h1", "h2", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td",
];

export function AgentReply({ text }: { text: string }) {
  return (
    <div className="reply">
      <Markdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        allowedElements={ALLOWED}
        unwrapDisallowed
      >
        {text}
      </Markdown>
    </div>
  );
}
