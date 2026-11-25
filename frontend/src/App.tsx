import { lazy, Suspense } from "react";
import { Route, Routes } from "react-router-dom";
import Layout from "./components/Layout";
import UploadPage from "./pages/Upload";
import StatementsPage from "./pages/Statements";

// charts (Recharts) only load on the pages that use them
const StatementPage = lazy(() => import("./pages/Statement"));
const ChatPage = lazy(() => import("./pages/Chat"));
const EvalsPage = lazy(() => import("./pages/Evals"));

export default function App() {
  return (
    <Layout>
      <Suspense fallback={<span className="loading loading-dots" />}>
        <Routes>
          <Route path="/" element={<UploadPage />} />
          <Route path="/statements" element={<StatementsPage />} />
          <Route path="/statements/:id" element={<StatementPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/evals" element={<EvalsPage />} />
          <Route path="*" element={<div className="alert">Page not found.</div>} />
        </Routes>
      </Suspense>
    </Layout>
  );
}
