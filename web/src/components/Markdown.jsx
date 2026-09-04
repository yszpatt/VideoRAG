import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** 笔记 Markdown 渲染：只支持笔记里会出现的元素，样式统一走 styles.css */
export default function Markdown({ children, className = "" }) {
  if (!children) return null;
  return (
    <div className={`md ${className}`}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          a: ({ node, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer" />
          ),
          table: ({ node, ...props }) => (
            <div className="md-table-wrap">
              <table {...props} />
            </div>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
