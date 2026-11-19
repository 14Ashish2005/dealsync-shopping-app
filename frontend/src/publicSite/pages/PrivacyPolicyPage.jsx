import PageMeta from '../components/PageMeta.jsx'

export default function PrivacyPolicyPage() {
  return (
    <article className="content-page page-container legal-page">
      <PageMeta
        title="Privacy Policy — DealSync"
        description="Privacy information for the DealSync website and sign-in-gated shopping dashboard."
      />
      <div className="page-heading">
        <span className="eyebrow">PRIVACY</span>
        <h1>Privacy Policy</h1>
        <p>
          This policy covers the DealSync public pages and sign-in-gated
          shopping dashboard, operated by DealSync Project Team. Effective
          date: 18 November 2026.
        </p>
      </div>

      <div className="prose-column">
        <section className="prose-section">
          <h2>Scope</h2>
          <p>
            This policy applies to the public pages and dashboard served from
            this website. The dashboard uses Google sign-in and the existing
            DealSync backend for shopping features.
          </p>
          <p>
            The dashboard supports product and image search, AI chat,
            comparisons, carts, and demo order history. No payment is collected
            and no actual retailer purchase is made. Google and external
            providers may handle information under their own privacy policies.
          </p>
        </section>

        <section className="prose-section">
          <h2>Information and contact</h2>
          <p>
            This website has no in-site contact form. Its contact link opens
            your email application. If you choose to email the team, the team
            receives the email address and message content you provide so it
            can respond to your inquiry. Your email service also handles that
            message.
          </p>
        </section>

        <section className="prose-section">
          <h2>Hosting and technical information</h2>
          <p>
            The production hosting configuration and any provider-specific
            technical logging have not been verified. This information will be
            updated when the production configuration is confirmed.
          </p>
        </section>

        <section className="prose-section">
          <h2>Privacy questions</h2>
          <p>
            For privacy questions about DealSync, contact DealSync Project Team
            at{' '}
            <a href="mailto:dealsync06@gmail.com">dealsync06@gmail.com</a>.
          </p>
        </section>
      </div>
    </article>
  )
}
