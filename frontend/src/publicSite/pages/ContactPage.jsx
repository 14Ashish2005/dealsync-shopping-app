import PageMeta from '../components/PageMeta.jsx'

export default function ContactPage() {
  return (
    <article className="content-page page-container">
      <PageMeta
        title="Contact DealSync"
        description="Contact DealSync at dealsync06@gmail.com."
      />
      <div className="page-heading">
        <span className="eyebrow">CONTACT</span>
        <h1>We’d be glad to hear from you.</h1>
        <p>
          For questions or feedback about DealSync, email us at the address
          below.
        </p>
      </div>

      <section className="contact-card">
        <div className="contact-icon" aria-hidden="true">↗</div>
        <div className="contact-copy">
          <span className="section-label">PUBLIC CONTACT</span>
          <h2>Email DealSync</h2>
          <p>
            <strong>
              <a href="mailto:dealsync06@gmail.com">dealsync06@gmail.com</a>
            </strong>
          </p>
        </div>
      </section>
    </article>
  )
}
