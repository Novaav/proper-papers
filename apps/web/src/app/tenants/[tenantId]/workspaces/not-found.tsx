// One page for an unknown tenant, a malformed ID and a tenant the user is not
// a member of, so the response never reveals which tenants exist.
export default function TenantNotFound() {
  return (
    <main id="main" tabIndex={-1} className="card message">
      <h1>Tenant not found</h1>
      <p>This tenant does not exist or you do not have access to it.</p>
      <p>
        <a href="/account">Back to your account</a>
      </p>
    </main>
  );
}
