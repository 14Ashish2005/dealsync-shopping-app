import PageMeta from '../components/PageMeta.jsx'

export default function AboutPage() {
  return (
    <article className="content-page page-container">
      <PageMeta
        title="About DealSync"
        description="Learn about DealSync, an AI-powered, agentic shopping assistant and its vision for smarter shopping."
      />
      <div className="page-heading">
        <span className="eyebrow">ABOUT THE PROJECT</span>
        <h1>DealSync — Smarter Shopping. Powered by Intelligence.</h1>
        <p>
          DealSync is an AI-powered, agentic shopping assistant intended to
          bring product discovery, comparison, and cart organization into a
          more thoughtful shopping journey.
        </p>
      </div>

      <div className="prose-grid">
        <section className="prose-section">
          <span className="section-label">OUR VISION</span>
          <h2>Spend less time searching. Make more informed choices.</h2>
          <p>
            Our product vision brings together image-based product discovery,
            discovery across shopping platforms where access is authorized,
            intelligent product and price comparison, AI-assisted shopping
            decisions, and organizing selected products in a shopping cart.
          </p>
          <p>
            Bringing these steps together aims to reduce the time people spend
            searching across platforms and help them make informed purchasing
            decisions.
          </p>
        </section>

        <section className="prose-section">
          <span className="section-label">OUR MISSION</span>
          <h2>Make every shopping decision smarter.</h2>
          <p>
            Our mission is to make product research clearer, reduce the effort
            of comparing options, and support people as they decide what best
            fits their needs.
          </p>
        </section>

        <section className="prose-section">
          <span className="section-label">PRODUCT VISION AND CURRENT CAPABILITIES</span>
          <h2>Explore the shopping dashboard after signing in.</h2>
          <p>
            The public pages introduce the project. After Google sign-in, the
            dashboard provides product discovery, image search, AI chat,
            comparisons, a shopping cart, and demo order history.
          </p>
          <p>
            DealSync is a demo project: no payment is collected and no actual
            retailer purchase is made. Provider discovery depends on the
            integrations and access available to the project.
          </p>
        </section>
      </div>
    </article>
  )
}
