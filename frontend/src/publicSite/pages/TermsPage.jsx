import PageMeta from '../components/PageMeta.jsx'

export default function TermsPage() {
  return (
    <article className="content-page page-container legal-page">
      <PageMeta
        title="Terms and Conditions — DealSync"
        description="Terms for the DealSync website and shopping dashboard."
      />
      <div className="page-heading">
        <span className="eyebrow">TERMS</span>
        <h1>Terms and Conditions</h1>
        <p>
          These terms apply to the DealSync website and shopping dashboard.
          Effective date: 18 November 2026.
        </p>
      </div>

      <div className="prose-column">
        <section className="prose-section">
          <h2>Operator and scope</h2>
          <p>
            The operator is DealSync Project Team. Public pages provide
            project information; the shopping dashboard is available after
            Google sign-in.
          </p>
        </section>

        <section className="prose-section">
          <h2>Informational website and project vision</h2>
          <p>
            Public-page content is provided for general informational
            purposes. The dashboard includes product discovery, AI shopping
            assistance, product comparisons, a cart, and demo order history.
          </p>
          <p>
            DealSync is a demo project. It does not collect payment or make
            purchases from Amazon, Flipkart, or other retailers. Provider
            availability and offer information may vary.
          </p>
        </section>

        <section className="prose-section">
          <h2>Acceptable use</h2>
          <p>
            Use this website lawfully and do not use it to disrupt its
            availability, attempt unauthorized access to systems or data, or
            interfere with other visitors’ use of the website.
          </p>
        </section>

        <section className="prose-section">
          <h2>Contact</h2>
          <p>
            For questions about these terms or the website, contact DealSync
            Project Team at{' '}
            <a href="mailto:dealsync06@gmail.com">dealsync06@gmail.com</a>.
          </p>
        </section>
      </div>
    </article>
  )
}
