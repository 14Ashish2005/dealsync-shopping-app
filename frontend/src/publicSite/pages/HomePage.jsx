import { Link } from 'react-router-dom'
import PageMeta from '../components/PageMeta.jsx'

const principles = [
  {
    number: '01',
    title: 'Start with what matters',
    text: 'Shopping decisions are personal. DealSync aims to make priorities and questions easier to bring into the process.',
  },
  {
    number: '02',
    title: 'See the trade-offs',
    text: 'The project explores clearer ways to look at product information side by side, without pretending every choice is simple.',
  },
  {
    number: '03',
    title: 'Keep people in control',
    text: 'AI should support a shopper’s thinking—not make the decision or hide the limits of the information available.',
  },
]

export default function HomePage() {
  return (
    <>
      <PageMeta
        title="DealSync — thoughtful shopping, in sync"
        description="Meet DealSync, a team-operated project exploring how AI can help make shopping decisions clearer."
      />
      <section className="hero-section">
        <div className="hero-inner page-container">
          <div className="hero-copy">
            <span className="eyebrow">
              <span className="eyebrow-dot" aria-hidden="true" />
              A SHOPPING PROJECT IN PROGRESS
            </span>
            <h1>
              Make room for
              <br />
              <span>better decisions.</span>
            </h1>
            <p className="hero-description">
              DealSync is exploring how AI can make product research feel more
              considered—helping people organize questions, compare information,
              and focus on what matters to them.
            </p>
            <div className="hero-actions">
              <a className="button button-primary" href="/auth/google/login">
                Sign in to DealSync <span aria-hidden="true">→</span>
              </a>
              <Link className="text-link" to="/about">
                Get to know DealSync
              </Link>
              <Link className="text-link" to="/affiliate-disclosure">
                Our disclosure approach
              </Link>
            </div>
            <p className="hero-note">
              These public pages introduce the project. Sign in to open the
              DealSync shopping dashboard.
            </p>
          </div>
          <div
            className="hero-visual"
            role="img"
            aria-label="Illustration of shopping questions and comparison notes"
          >
            <div className="visual-glow" />
            <div className="visual-orbit visual-orbit-one" />
            <div className="visual-orbit visual-orbit-two" />
            <div className="visual-card visual-card-main">
              <div className="visual-card-top">
                <span className="visual-spark" aria-hidden="true">✦</span>
                <span>THE SHOPPING QUESTION</span>
                <span className="visual-menu" aria-hidden="true">···</span>
              </div>
              <p className="visual-question">
                What matters most
                <br />
                for this choice?
              </p>
              <div className="visual-divider" />
              <div className="visual-criteria">
                <span className="criteria-icon" aria-hidden="true">◎</span>
                <span>Compare with context</span>
                <span className="criteria-arrow" aria-hidden="true">↗</span>
              </div>
              <div className="visual-criteria">
                <span className="criteria-icon criteria-icon--soft" aria-hidden="true">◇</span>
                <span>Keep priorities in view</span>
                <span className="criteria-arrow" aria-hidden="true">↗</span>
              </div>
              <div className="visual-footnote">A product vision — not a live tool</div>
            </div>
            <div className="visual-chip visual-chip-top">
              <span className="chip-dot" />
              Clearer questions
            </div>
            <div className="visual-chip visual-chip-bottom">
              <span aria-hidden="true">✧</span>
              Thoughtful by design
            </div>
          </div>
        </div>
      </section>

      <section className="intro-section page-container">
        <div className="section-label">THE IDEA</div>
        <div className="intro-grid">
          <h2>
            Shopping research can feel like
            <span> too many tabs and too little clarity.</span>
          </h2>
          <div className="intro-copy">
            <p>
              Details, priorities, and questions can be scattered across a
              shopping journey. DealSync is a team-operated project exploring
              whether AI can help bring those pieces into a more understandable
              decision-making flow.
            </p>
            <p>
              This public website introduces that purpose. It does not provide
              shopping tools without signing in. The dashboard is a demo:
              payments are not collected and no retailer purchase is made.
            </p>
          </div>
        </div>
      </section>

      <section className="principles-section">
        <div className="page-container">
          <div className="section-heading">
            <div>
              <div className="section-label">WHAT WE’RE EXPLORING</div>
              <h2>A more thoughtful shopping experience.</h2>
            </div>
            <p>
              These are project goals, not a list of features available on this
              website.
            </p>
          </div>
          <div className="principle-grid">
            {principles.map((principle) => (
              <article className="principle-card" key={principle.number}>
                <div className="principle-topline">
                  <span>{principle.number}</span>
                  <span className="principle-status">AIM</span>
                </div>
                <h3>{principle.title}</h3>
                <p>{principle.text}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="closing-section page-container">
        <div className="closing-panel">
          <div className="closing-icon" aria-hidden="true">D</div>
          <div>
            <span className="section-label">A PROJECT BUILT BY A TEAM</span>
            <h2>Curious about the thinking behind DealSync?</h2>
            <p>
              Learn about the project’s purpose and the principles guiding its
              direction.
            </p>
          </div>
          <Link className="button button-light" to="/about">
            About the project <span aria-hidden="true">→</span>
          </Link>
        </div>
      </section>
    </>
  )
}
