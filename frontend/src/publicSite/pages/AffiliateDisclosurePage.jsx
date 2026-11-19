import PageMeta from '../components/PageMeta.jsx'

export default function AffiliateDisclosurePage() {
  return (
    <article className="content-page page-container">
      <PageMeta
        title="Affiliate Disclosure — DealSync"
        description="DealSync does not claim affiliate approval; any future affiliate relationships and links will be disclosed before use."
      />
      <div className="page-heading">
        <span className="eyebrow">TRANSPARENCY</span>
        <h1>Affiliate Disclosure</h1>
        <p>
          Clear information about affiliate relationships matters to us.
        </p>
      </div>

      <section className="disclosure-card">
        <div className="disclosure-symbol" aria-hidden="true">↗</div>
        <span className="section-label">OUR CURRENT STATUS</span>
        <h2>No affiliate links are used on this website.</h2>
        <p>
          DealSync is not claiming to be an Amazon Associate or to be approved
          by any affiliate program. This standalone public site contains no
          affiliate links.
        </p>
        <p>
          If the team establishes an affiliate relationship and decides to use
          affiliate links in the future, the relationship and relevant links
          will be clearly disclosed before they are used. Any future wording
          will be reviewed against the applicable program requirements.
        </p>
      </section>
    </article>
  )
}
