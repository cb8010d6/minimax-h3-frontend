import { useState } from "react";
import { Navigate, NavLink, Route, Routes } from "react-router-dom";
import { useCurrentUser } from "./api/queries";
import type { GenerationJobDetail } from "./api/types";
import { AdminLayout, CatalogScreen, InvitesScreen } from "./features/admin";
import { LoginScreen } from "./features/auth";
import { ProjectBoard, ProjectListScreen } from "./features/director";
import { GenerateScreen } from "./features/generate";
import { JobModal, QueueSidebar } from "./features/queue";
import { LogOutIcon } from "./features/shared/Icon";
import { LanguageToggle, useI18n } from "./i18n";
import "./App.css";

function MainLayout() {
  const [selectedJobId, setSelectedJobId] = useState<number | null>(null);
  const [redoPayload, setRedoPayload] = useState<GenerationJobDetail | null>(null);
  // Only meaningful below the .app-layout column-stack breakpoint (900px,
  // see App.css) -- above it both panes always show side by side and this
  // switcher stays hidden. Below it, .queue-sidebar used to render after
  // the entire Generate form in document order, so checking on a render
  // meant scrolling past the whole form first (owner-reported pain point).
  // The className below toggles which pane is display:none at that
  // breakpoint; nothing here affects layout above it.
  const [mobileView, setMobileView] = useState<"generate" | "queue">("generate");

  function handleRedo(job: GenerationJobDetail) {
    setRedoPayload(job);
    setSelectedJobId(null);
  }

  return (
    <>
      <div className="mobile-view-switcher" role="tablist" aria-label="View">
        <button
          type="button"
          className={`tab ${mobileView === "generate" ? "selected" : ""}`}
          aria-selected={mobileView === "generate"}
          onClick={() => setMobileView("generate")}
        >
          Generate
        </button>
        <button
          type="button"
          className={`tab ${mobileView === "queue" ? "selected" : ""}`}
          aria-selected={mobileView === "queue"}
          onClick={() => setMobileView("queue")}
        >
          Queue
        </button>
      </div>
      <div className={`app-layout mobile-view-${mobileView}`}>
        <GenerateScreen redoJob={redoPayload} onRedoConsumed={() => setRedoPayload(null)} />
        <QueueSidebar onOpenJob={setSelectedJobId} />
      </div>
      {selectedJobId != null && (
        <JobModal jobId={selectedJobId} onClose={() => setSelectedJobId(null)} onRedo={handleRedo} />
      )}
    </>
  );
}

function App() {
  const me = useCurrentUser();
  const { t } = useI18n();

  if (me.isLoading) {
    return (
      <section id="center">
        <p>{t("common.loading", "Loading…")}</p>
      </section>
    );
  }

  if (me.isError) {
    return (
      <section id="center">
        <p className="error">{t("app.loadingError", "Couldn't reach the server. Try reloading.")}</p>
      </section>
    );
  }

  if (!me.data?.authenticated) {
    return <LoginScreen />;
  }

  return (
    <>
      <nav className="app-nav">
        <div className="app-brand">
          <span className="app-mark" aria-hidden="true">
            M3
          </span>
           <span className="app-title">{t("app.title", "Minimax H3")}</span>
        </div>
        <div className="app-nav-links">
          <NavLink to="/" end>
            {t("nav.generate", "Generate")}
          </NavLink>
          <NavLink to="/director">{t("nav.director", "Director")}</NavLink>
          {me.data.is_staff && <NavLink to="/manage">{t("nav.admin", "Admin")}</NavLink>}
        </div>
        <LanguageToggle />
        <span className="app-user">
          <span className="app-user-avatar" aria-hidden="true">
            {(me.data.username ?? "?").slice(0, 1).toUpperCase()}
          </span>
          {me.data.username}
          <a className="app-logout" href="/accounts/logout/" title="Log out" aria-label="Log out">
            <LogOutIcon size={16} />
          </a>
        </span>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<MainLayout />} />
          <Route path="/director" element={<ProjectListScreen />} />
          <Route path="/director/:projectId" element={<ProjectBoard />} />
          <Route
            path="/manage"
            element={me.data.is_staff ? <AdminLayout /> : <Navigate to="/" replace />}
          >
            <Route index element={<Navigate to="invites" replace />} />
            <Route path="invites" element={<InvitesScreen />} />
            <Route path="catalog" element={<CatalogScreen />} />
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}

export default App;
