// One page for an unknown workspace, a workspace in a tenant the user is not a
// member of, and malformed IDs, so the response never reveals what exists.
export default function WorkspaceNotFound() {
  return (
    <main id="main" tabIndex={-1} className="card message">
      <h1>Workspace not found</h1>
      <p>This workspace does not exist or you do not have access to it.</p>
      <p>
        <a href="/account">Back to your account</a>
      </p>
    </main>
  );
}
