export default function ApisPage() {
  return (
    <>
      <div className="topbar">
        <div>
          <span className="h">APIs</span>{" "}
          <span className="sub">proxy-only secrets · coming soon</span>
        </div>
      </div>
      <div className="content">
        <div className="state">
          API key vaulting is on the way.
          <span className="mono">
            Keys will be stored proxy-only and never enter model context.
          </span>
        </div>
      </div>
    </>
  );
}
