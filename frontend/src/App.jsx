import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import ExternalCart from './components/ExternalCart'
import './App.css'

const navItems = [
  { label: 'Dashboard', icon: '▦' },
  { label: 'Products', icon: '⌕' },
  { label: 'Compare', icon: '⇄' },
  { label: 'AI Assistant', icon: '✧' },
  { label: 'Shop by Image', icon: '▧' },
  { label: 'My Cart', icon: '🛒' },
  { label: 'My Orders', icon: '▤' },
]

const MAX_IMAGE_FILE_BYTES = 10 * 1024 * 1024
const IMAGE_FILE_TYPES = {
  '.jpg': ['image/jpeg', 'image/jpg'],
  '.jpeg': ['image/jpeg', 'image/jpg'],
  '.png': ['image/png'],
  '.webp': ['image/webp'],
}
const IMAGE_SEARCH_CURRENCIES = ['AUD', 'CAD', 'EUR', 'GBP', 'INR', 'JPY', 'USD']

async function readApiResponse(response, requestName) {
  let data

  try {
    data = await response.json()
  } catch {
    throw new Error(`${requestName} returned an invalid response.`)
  }

  if (!response.ok) {
    const error = new Error(
      typeof data?.detail === 'string'
        ? data.detail
        : `${requestName} failed (HTTP ${response.status}).`,
    )
    error.status = response.status
    error.retryAfter = response.headers.get('Retry-After')
    throw error
  }

  return data
}

function parseRetryAfterSeconds(value) {
  const retryAfter = typeof value === 'string' ? value.trim() : ''
  if (!/^\d+$/.test(retryAfter)) return null

  const seconds = Number(retryAfter)
  return Number.isSafeInteger(seconds) && seconds > 0 ? seconds : null
}

function isSmartSearchResponse(data) {
  const isRecord = (value) =>
    value !== null && typeof value === 'object' && !Array.isArray(value)
  const isOffer = (offer) =>
    isRecord(offer) &&
    typeof offer.offer_id === 'string' &&
    typeof offer.provider_key === 'string' &&
    typeof offer.product_name === 'string'

  return (
    isRecord(data) &&
    typeof data.original_query === 'string' &&
    typeof data.normalized_search_query === 'string' &&
    (data.product_category === null ||
      typeof data.product_category === 'string') &&
    (data.budget_amount === null ||
      (typeof data.budget_amount === 'number' &&
        Number.isFinite(data.budget_amount))) &&
    (data.budget_currency === null ||
      typeof data.budget_currency === 'string') &&
    Array.isArray(data.preferences) &&
    data.preferences.every((value) => typeof value === 'string') &&
    Array.isArray(data.unsupported_requirements) &&
    data.unsupported_requirements.every((value) => typeof value === 'string') &&
    typeof data.clarification_required === 'boolean' &&
    Array.isArray(data.offers) &&
    data.offers.every(isOffer) &&
    Array.isArray(data.providers) &&
    data.providers.every(
      (provider) =>
        isRecord(provider) &&
        typeof provider.provider_key === 'string' &&
        typeof provider.status === 'string',
    ) &&
    typeof data.partial === 'boolean' &&
    (data.clarification_message === undefined ||
      typeof data.clarification_message === 'string')
  )
}

async function fetchExternalCartSnapshot() {
  const response = await fetch('/api/cart/external', {
    method: 'GET',
    credentials: 'same-origin',
  })
  const data = await readApiResponse(response, 'Provider cart request')
  if (
    !Array.isArray(data.items) ||
    !data.items.every(
      (item) =>
        item &&
        typeof item.offer_id === 'string' &&
        Number.isSafeInteger(item.quantity),
    ) ||
    !Array.isArray(data.currency_totals)
  ) {
    throw new Error('The provider cart returned an unexpected response.')
  }
  return data
}

function providerCartErrorMessage(error) {
  const statusMessages = {
    401: 'Sign in again to add an offer to Provider Offers.',
    403: 'The request was blocked by origin protection.',
    404: 'The exact provider offer could not be found.',
    409: 'The offer is stale, unavailable, or otherwise not eligible.',
    422: 'The add request was rejected as invalid.',
    503: 'The provider cart or exact-offer provider is unavailable.',
    504: 'Exact-offer resolution timed out.',
  }

  if (statusMessages[error?.status]) return statusMessages[error.status]
  if (error instanceof TypeError) {
    return 'The add result could not be confirmed because the network request failed.'
  }
  return 'The provider cart request could not be completed.'
}

function validExternalImageUrl(value) {
  if (typeof value !== 'string' || value.trim() !== value) return null

  try {
    const url = new URL(value)
    return url.protocol === 'https:' &&
      url.hostname &&
      !url.username &&
      !url.password
      ? value
      : null
  } catch {
    return null
  }
}

function validLocalImagePath(value) {
  if (
    typeof value !== 'string' ||
    !value.startsWith('/images/products/') ||
    value.trim() !== value ||
    /[\\%?#:\s]/.test(value) ||
    value.split('/').some((part) => part === '.' || part === '..')
  ) {
    return null
  }

  return /\.(?:jpe?g|png|svg|webp)$/i.test(value) ? value : null
}

function isCartPreview(value) {
  return (
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value) &&
    Array.isArray(value.items) &&
    Number.isInteger(value.eligible_count) &&
    Number.isInteger(value.ineligible_count) &&
    value.items.every(
      (item) =>
        item !== null &&
        typeof item === 'object' &&
        typeof item.provider_key === 'string' &&
        typeof item.provider_name === 'string' &&
        typeof item.provider_product_id === 'string' &&
        typeof item.product_name === 'string' &&
        typeof item.price_display === 'string' &&
        (item.currency === null || typeof item.currency === 'string') &&
        typeof item.eligible === 'boolean' &&
        (item.eligibility_reason === null ||
          typeof item.eligibility_reason === 'string') &&
        typeof item.is_demo_sample === 'boolean',
    )
  )
}

function ProductImage({ product }) {
  const imageSources = [
    validExternalImageUrl(product.image_url),
    validLocalImagePath(product.local_image_url),
    validLocalImagePath(product.fallback_image),
    '/images/products/default.svg',
  ].filter((source, index, sources) => source && sources.indexOf(source) === index)
  const [sourceIndex, setSourceIndex] = useState(0)
  const [isLoaded, setIsLoaded] = useState(false)
  const [isUnavailable, setIsUnavailable] = useState(false)
  const source = imageSources[sourceIndex]

  return (
    <>
      {source && !isUnavailable ? (
        <img
          className={`product-image${isLoaded ? ' loaded' : ''}`}
          src={source}
          alt={product.name || 'Product'}
          loading="lazy"
          decoding="async"
          aria-busy={!isLoaded}
          onLoad={() => setIsLoaded(true)}
          onError={() => {
            setIsLoaded(false)
            if (sourceIndex + 1 < imageSources.length) {
              setSourceIndex((index) => index + 1)
            } else {
              setIsUnavailable(true)
            }
          }}
        />
      ) : (
        <span className="product-image-unavailable" role="img" aria-label={`${product.name || 'Product'} image unavailable`}>
          Image unavailable
        </span>
      )}
    </>
  )
}

function ProductCard({ product, onAdd, canAdd, isAdding }) {
  const hasPrice =
    typeof product.price === 'number' && Number.isFinite(product.price)

  return (
    <article className="product-card">
      <div className="product-visual">
        <ProductImage
          key={`${product.id}-${product.image_url}-${product.local_image_url}-${product.fallback_image}`}
          product={product}
        />
        {product.is_organic && <span className="product-tag">Organic</span>}
      </div>

      <div className="product-details">
        {product.category && (
          <span className="product-category">{product.category}</span>
        )}
        <h3>{product.name}</h3>
        {product.description && (
          <p className="product-description">{product.description}</p>
        )}

        <div className="product-footer">
          <div className="price">
            {formatMajorPrice(product.price, product.currency)}
          </div>
          <button
            className="add-button"
            onClick={() => onAdd(product)}
            disabled={!hasPrice || !canAdd || isAdding}
            aria-label={
              hasPrice
                ? `Add ${product.name} to cart`
                : `${product.name}: price unavailable`
            }
          >
            {isAdding ? '…' : '+'}
          </button>
        </div>
      </div>
    </article>
  )
}

function formatMajorPrice(value, currency) {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return 'Price unavailable'
  }
  if (!currency) return `${value.toFixed(2)} (currency unverified)`

  try {
    return new Intl.NumberFormat('en-IN', {
      style: 'currency',
      currency,
    }).format(value)
  } catch {
    return `${value.toFixed(2)} ${currency} (format unavailable)`
  }
}

function formatOfferPrice(priceMinor, currency) {
  if (priceMinor == null && currency == null) return 'Currency unverified'
  if (
    !Number.isSafeInteger(priceMinor) ||
    priceMinor < 0 ||
    typeof currency !== 'string'
  ) {
    return 'Price unavailable'
  }

  try {
    const exponent = new Intl.NumberFormat('en', {
      style: 'currency',
      currency,
    }).resolvedOptions().maximumFractionDigits
    return formatMajorPrice(priceMinor / (10 ** exponent), currency)
  } catch {
    return 'Currency unavailable'
  }
}

function AccountControl({
  isAuthenticated,
  signOutPending,
  signOutError,
  onSignIn,
  onSignOut,
}) {
  const [isOpen, setIsOpen] = useState(false)
  const containerRef = useRef(null)
  const triggerRef = useRef(null)

  useEffect(() => {
    if (!isOpen) return undefined

    function handlePointerDown(event) {
      if (!containerRef.current?.contains(event.target)) {
        setIsOpen(false)
      }
    }

    function handleKeyDown(event) {
      if (event.key !== 'Escape') return
      setIsOpen(false)
      triggerRef.current?.focus()
    }

    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [isOpen])

  return (
    <div className="account-control" ref={containerRef}>
      <button
        ref={triggerRef}
        className={`account-trigger ${isAuthenticated ? 'account-trigger-signed-in' : ''}`}
        type="button"
        onClick={
          isAuthenticated
            ? () => setIsOpen((current) => !current)
            : onSignIn
        }
        aria-label={isAuthenticated ? 'Open account options' : 'Sign in with Google'}
        aria-expanded={isAuthenticated ? isOpen : undefined}
        aria-controls={isAuthenticated && isOpen ? 'account-dropdown' : undefined}
      >
        <span className="account-avatar" aria-hidden="true">G</span>
        <span className="account-trigger-label">
          {isAuthenticated ? 'Account' : 'Sign in'}
        </span>
      </button>

      {isAuthenticated && isOpen && (
        <div
          className="account-dropdown"
          id="account-dropdown"
          role="group"
          aria-label="Account options"
        >
          <div className="account-identity">
            <span className="account-avatar account-avatar-large" aria-hidden="true">
              G
            </span>
            <div className="account-identity-text">
              <strong>Google account</strong>
              <span>Signed in</span>
            </div>
          </div>
          <p className="account-profile-note">
            Name, email, and profile photo are not available from this sign-in response.
          </p>
          {signOutError && (
            <p className="account-signout-error" role="alert">
              {signOutError}
            </p>
          )}
          <button
            className="account-signout-button"
            type="button"
            onClick={onSignOut}
            disabled={signOutPending}
          >
            {signOutPending ? 'Signing out...' : 'Sign out'}
          </button>
        </div>
      )}
    </div>
  )
}

function DemoOfferNotice() {
  return (
    <div className="demo-offer-notice" role="note">
      <span className="demo-badge">DEMO</span>
      <span>Sample data — not a live marketplace price.</span>
    </div>
  )
}

function ProviderBadge({ providerKey, providerName }) {
  const providerType =
    providerKey === 'demo_catalog'
      ? 'demo'
      : providerKey === 'local_catalog'
        ? 'local'
        : 'other'

  return (
    <span className={`provider-badge provider-badge-${providerType}`}>
      {providerName}
    </span>
  )
}

function CartPreviewCard({ preview, canConfirm, isLoading, onConfirm }) {
  if (!preview || !Array.isArray(preview.items)) return null

  return (
    <div className="cart-preview-card">
      <ul className="cart-preview-items">
        {preview.items.map((item) => (
          <li
            className={item.eligible ? 'eligible' : 'ineligible'}
            key={`${item.provider_key}:${item.provider_product_id}`}
          >
            <strong>{item.product_name}</strong>
            <span>{item.provider_name}</span>
            <span>
              {item.price_display}
              {item.currency ? ` · ${item.currency}` : ' · Currency unverified'}
            </span>
            <span className="cart-preview-eligibility">
              {item.eligible
                ? 'Eligible for the persistent local cart'
                : `Ineligible: ${item.eligibility_reason}`}
            </span>
            {item.is_demo_sample && (
              <span className="cart-preview-demo-note">
                Sample data — not a live marketplace price.
              </span>
            )}
          </li>
        ))}
      </ul>
      {canConfirm && preview.eligible_count > 0 && (
        <button
          className="cart-preview-confirm"
          type="button"
          disabled={isLoading}
          onClick={onConfirm}
        >
          Confirm adding {preview.eligible_count} eligible product(s)
        </button>
      )}
    </div>
  )
}

function comparisonReferenceKey(reference) {
  if (
    !reference ||
    typeof reference.provider_key !== 'string' ||
    typeof reference.provider_product_id !== 'string'
  ) {
    return ''
  }
  return JSON.stringify([reference.provider_key, reference.provider_product_id])
}

function isComparisonResponse(data, expectedReferences) {
  if (
    !data ||
    !Array.isArray(data.candidates) ||
    !Array.isArray(data.price_summaries) ||
    !data.recommendation ||
    !['recommended', 'no_clear_winner', 'insufficient_evidence'].includes(
      data.recommendation.status,
    ) ||
    !Array.isArray(data.reasons) ||
    !Array.isArray(data.limitations)
  ) {
    return false
  }

  const actualReferences = data.candidates.map((candidate) =>
    comparisonReferenceKey(candidate?.reference),
  )
  const expectedKeys = expectedReferences.map(comparisonReferenceKey)
  if (
    actualReferences.some((key) => !key) ||
    new Set(actualReferences).size !== actualReferences.length ||
    actualReferences.length !== expectedKeys.length ||
    expectedKeys.some((key) => !actualReferences.includes(key))
  ) {
    return false
  }

  return data.candidates.every(
    (candidate) =>
      candidate.offer &&
      typeof candidate.offer.product_name === 'string' &&
      candidate.evidence &&
      typeof candidate.evidence === 'object' &&
      Array.isArray(candidate.known_attributes) &&
      Array.isArray(candidate.unknown_attributes) &&
      Array.isArray(candidate.must_have_evaluations) &&
      ['eligible', 'ineligible', 'undetermined'].includes(candidate.eligibility),
  ) && data.price_summaries.every(
    (summary) =>
      typeof summary.currency === 'string' &&
      Number.isSafeInteger(summary.minimum_price_minor) &&
      Number.isSafeInteger(summary.maximum_price_minor) &&
      Array.isArray(summary.candidate_references),
  ) && (
    data.recommendation.candidate == null ||
    actualReferences.includes(
      comparisonReferenceKey(data.recommendation.candidate),
    )
  )
}

function displayEvidenceValue(value) {
  if (value == null) return 'Unknown'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (typeof value === 'object') {
    if (
      Number.isSafeInteger(value.amount_minor) &&
      typeof value.currency === 'string'
    ) {
      return formatOfferPrice(value.amount_minor, value.currency)
    }
    return JSON.stringify(value)
  }
  return String(value)
}

function formatOrderDate(value) {
  if (typeof value !== 'string' || value.length === 0) return 'Date unavailable'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? 'Date unavailable' : date.toLocaleString()
}

function ComparisonResultView({
  result,
  providers,
  providersLoading,
  providerAddBusyOfferId,
  providerAddNotice,
  providerCartSyncRequired,
  onAddToProviderCart,
  onRetryProviderCartSync,
  onSignIn,
}) {
  const recommendation = result.recommendation
  const hasDemoCandidates = result.candidates.some(
    (candidate) => candidate.offer?.provider_key === 'demo_catalog',
  )
  const recommendedCandidate = recommendation.candidate
    ? result.candidates.find(
        (candidate) =>
          comparisonReferenceKey(candidate.reference) ===
          comparisonReferenceKey(recommendation.candidate),
      )
    : null
  const recommendationTitle =
    recommendation.status === 'recommended' && recommendedCandidate
      ? 'Recommended match'
      : recommendation.status === 'insufficient_evidence'
        ? 'Insufficient evidence'
        : 'No clear winner'

  return (
    <div className="comparison-result" aria-live="polite">
      <section
        className={`comparison-recommendation ${recommendation.status}`}
        aria-labelledby="comparison-recommendation-title"
      >
        <div>
          <span className="eyebrow">DETERMINISTIC RESULT</span>
          <h2 id="comparison-recommendation-title">{recommendationTitle}</h2>
          {recommendedCandidate && (
            <p>
              {recommendedCandidate.offer.product_name} uniquely met the supplied
              requirements.
            </p>
          )}
          {recommendation.status === 'insufficient_evidence' && (
            <p>A recommendation was withheld because required evidence is unknown or incomparable.</p>
          )}
          {recommendation.status === 'no_clear_winner' && (
            <p>The available evidence does not establish one qualifying product.</p>
          )}
          {recommendation.basis?.length > 0 && (
            <p className="comparison-basis">
              {hasDemoCandidates ? 'Comparison basis' : 'Verified basis'}:{' '}
              {recommendation.basis.join(', ')}
            </p>
          )}
        </div>
        <span className="comparison-status-badge">{recommendation.status.replaceAll('_', ' ')}</span>
      </section>

      {result.reasons.length > 0 && (
        <section className="comparison-notes">
          <h3>Why this result</h3>
          <ul>{result.reasons.map((reason, index) => <li key={`${reason}-${index}`}>{reason}</li>)}</ul>
        </section>
      )}

      <section className="comparison-candidates" aria-label="Product comparison results">
        {result.candidates.map((candidate) => {
          const offer = candidate.offer
          const providerName =
            providers.find(
              (provider) => provider.provider_key === offer.provider_key,
            )?.display_name || offer.provider_key
          const rating = candidate.evidence.average_rating
          const reviewCount = candidate.evidence.review_count
          const knownEntries = Object.entries(candidate.evidence).filter(
            ([, evidence]) => evidence.status === 'known',
          )
          const requirements = [
            ...candidate.must_have_evaluations,
            ...(candidate.budget_evaluation ? [candidate.budget_evaluation] : []),
          ]

          return (
            <article
              className="comparison-candidate"
              key={comparisonReferenceKey(candidate.reference)}
            >
              <header className="comparison-candidate-heading">
                <ProviderBadge
                  providerKey={offer.provider_key}
                  providerName={providerName}
                />
                <h3>{offer.product_name}</h3>
                {offer.provider_key === 'demo_catalog' && <DemoOfferNotice />}
                <p>
                  Product reference: {candidate.reference.provider_product_id}
                </p>
              </header>

              <ProviderCartAddAction
                offer={offer}
                providers={providers}
                providersLoading={providersLoading}
                busyOfferId={providerAddBusyOfferId}
                notice={
                  providerAddNotice?.offerId === offer.offer_id
                    ? providerAddNotice
                    : null
                }
                syncRequired={providerCartSyncRequired}
                onAdd={onAddToProviderCart}
                onRetrySync={onRetryProviderCartSync}
                onSignIn={onSignIn}
              />

              <dl className="comparison-facts">
                <div>
                  <dt>Provider / store</dt>
                  <dd>{providerName}</dd>
                </div>
                <div>
                  <dt>
                    {offer.provider_key === 'demo_catalog'
                      ? 'Sample price'
                      : 'Verified price'}
                  </dt>
                  <dd>
                    {offer.price_minor == null || offer.currency == null
                      ? 'Price or currency unknown'
                      : formatOfferPrice(offer.price_minor, offer.currency)}
                  </dd>
                </div>
                <div>
                  <dt>Brand</dt>
                  <dd>{offer.brand || 'Unknown'}</dd>
                </div>
                <div>
                  <dt>Model</dt>
                  <dd>{offer.model || 'Unknown'}</dd>
                </div>
                <div>
                  <dt>Rating</dt>
                  <dd>
                    {rating?.status === 'known'
                      ? `${displayEvidenceValue(rating.value)} / 5`
                      : rating?.reason === 'no_reviews'
                        ? 'Unknown — no reviews'
                        : 'Unknown'}
                  </dd>
                </div>
                <div>
                  <dt>Review count</dt>
                  <dd>
                    {reviewCount?.status === 'known'
                      ? displayEvidenceValue(reviewCount.value)
                      : 'Unknown'}
                  </dd>
                </div>
                <div>
                  <dt>Eligibility</dt>
                  <dd>{candidate.eligibility}</dd>
                </div>
              </dl>

              <section className="comparison-attribute-section">
                <h4>Known attributes</h4>
                {knownEntries.length > 0 ? (
                  <ul className="comparison-evidence-list">
                    {knownEntries.map(([attribute, evidence]) => (
                      <li key={attribute}>
                        <strong>{attribute.replaceAll('_', ' ')}</strong>
                        <span>{displayEvidenceValue(evidence.value)}</span>
                        <small>Source: {evidence.source}</small>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>No attributes are verified for this product.</p>
                )}
              </section>

              <section className="comparison-attribute-section">
                <h4>Unknown attributes</h4>
                {candidate.unknown_attributes.length > 0 ? (
                  <ul className="comparison-unknown-list">
                    {candidate.unknown_attributes.map((attribute) => (
                      <li key={attribute}>{attribute.replaceAll('_', ' ')}</li>
                    ))}
                  </ul>
                ) : (
                  <p>No unknown attributes were reported.</p>
                )}
              </section>

              <section className="comparison-attribute-section">
                <h4>Requirement results</h4>
                {requirements.length > 0 ? (
                  <ul className="comparison-evidence-list">
                    {requirements.map((evaluation, index) => (
                      <li key={`${evaluation.attribute}-${index}`}>
                        <strong>{evaluation.attribute.replaceAll('_', ' ')}</strong>
                        <span>
                          {evaluation.status.replaceAll('_', ' ')}
                          {evaluation.expected_value !== undefined &&
                            ` · expected ${displayEvidenceValue(evaluation.expected_value)}`}
                        </span>
                        {evaluation.reason && <small>{evaluation.reason.replaceAll('_', ' ')}</small>}
                        <small>Source: {evaluation.evidence.source}</small>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>No must-have requirements or budget were provided.</p>
                )}
              </section>

              <details className="comparison-source-details">
                <summary>Evidence sources</summary>
                <ul>
                  {Object.entries(candidate.evidence).map(([attribute, evidence]) => (
                    <li key={attribute}>
                      <strong>{attribute.replaceAll('_', ' ')}</strong>
                      <span>{evidence.status}</span>
                      <small>{evidence.source}</small>
                      {evidence.reason && <small>{evidence.reason.replaceAll('_', ' ')}</small>}
                    </li>
                  ))}
                </ul>
              </details>
            </article>
          )
        })}
      </section>

      <section className="comparison-price-groups" aria-labelledby="comparison-price-title">
        <h3 id="comparison-price-title">Price summaries by currency</h3>
        {hasDemoCandidates && <DemoOfferNotice />}
        {result.price_summaries.length > 0 ? (
          <ul>
            {result.price_summaries.map((summary) => (
              <li key={summary.currency}>
                <strong>{summary.currency}</strong>
                <span>
                  {formatOfferPrice(summary.minimum_price_minor, summary.currency)}
                  {' – '}
                  {formatOfferPrice(summary.maximum_price_minor, summary.currency)}
                </span>
                <small>
                  {summary.offer_count}{' '}
                  {hasDemoCandidates ? 'offer(s)' : 'verified offer(s)'}; no conversion applied
                </small>
              </li>
            ))}
          </ul>
        ) : (
          <p>There are no numeric price summaries with verified currencies.</p>
        )}
      </section>

      {result.limitations.length > 0 && (
        <section className="comparison-notes">
          <h3>Limitations</h3>
          <ul>{result.limitations.map((limitation, index) => <li key={`${limitation}-${index}`}>{limitation}</li>)}</ul>
        </section>
      )}
    </div>
  )
}

function ProviderCartAddAction({
  offer,
  providers,
  providersLoading,
  busyOfferId,
  notice,
  syncRequired,
  onAdd,
  onRetrySync,
  onSignIn,
}) {
  if (offer?.provider_key === 'demo_catalog') return null

  const provider = providers.find(
    (item) => item.provider_key === offer?.provider_key,
  )
  const hasExactOfferId =
    typeof offer?.offer_id === 'string' && offer.offer_id.trim().length > 0
  const isLocalCatalog = offer?.provider_key === 'local_catalog'
  const isBusy = busyOfferId !== null
  const isBusyOffer = busyOfferId === offer?.offer_id
  const canAdd =
    !providersLoading &&
    !syncRequired &&
    !isBusy &&
    !isLocalCatalog &&
    hasExactOfferId &&
    provider?.supports_external_cart === true

  let disabledReason = ''
  if (syncRequired) {
    disabledReason = 'Reload Provider Offers before attempting another add.'
  } else if (providersLoading) {
    disabledReason = 'Checking provider eligibility...'
  } else if (isLocalCatalog) {
    disabledReason = 'Local catalog offers are not eligible for Provider Offers.'
  } else if (!hasExactOfferId) {
    disabledReason = 'This result has no exact provider offer ID.'
  } else if (!provider) {
    disabledReason = 'The provider is not in the backend registry.'
  } else if (!provider.supports_external_cart) {
    disabledReason = 'No eligible external-cart provider is currently enabled.'
  } else if (isBusy && !isBusyOffer) {
    disabledReason = 'Another provider offer is being added.'
  }

  return (
    <div className="provider-cart-add-action">
      <button
        className="provider-cart-add-button"
        type="button"
        disabled={!canAdd}
        onClick={() => onAdd(offer.offer_id)}
      >
        {isBusyOffer ? 'Adding...' : 'Add to Provider Offers'}
      </button>
      {disabledReason && (
        <p className="provider-cart-add-explanation" role="status">
          {disabledReason}
        </p>
      )}
      {syncRequired && notice && (
        <button
          className="provider-cart-sync-button"
          type="button"
          disabled={isBusy}
          onClick={() => onRetrySync(offer.offer_id)}
        >
          Retry cart synchronization
        </button>
      )}
      {notice && (
        <div
          className={
            notice.isError
              ? 'provider-cart-add-notice error'
              : 'provider-cart-add-notice success'
          }
          role={notice.isError ? 'alert' : 'status'}
        >
          <span>{notice.message}</span>
          {notice.status === 401 && (
            <button type="button" className="text-button" onClick={onSignIn}>
              Sign in again
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function OfferCard({
  offer,
  providerName,
  ranking,
  onToggleComparison,
  isSelected = false,
  selectionDisabled = false,
  discoveryPartial = false,
  providers = [],
  providersLoading = false,
  providerAddBusyOfferId = null,
  providerAddNotice = null,
  providerCartSyncRequired = false,
  onAddToProviderCart,
  onRetryProviderCartSync,
  onSignIn,
}) {
  const isDemoOffer = offer?.provider_key === 'demo_catalog'
  const imageProduct = {
    name: offer.product_name,
    image_url: offer.image_url,
  }
  const productUrl = validExternalImageUrl(offer.product_url)
  return (
    <article className="product-card offer-card">
      <div className="product-visual">
        <ProductImage
          key={offer.offer_id}
          product={imageProduct}
        />
      </div>
      <div className="product-details">
        <ProviderBadge
          providerKey={offer.provider_key}
          providerName={providerName}
        />
        <h3>{offer.product_name || 'Name unavailable'}</h3>
        {isDemoOffer && <DemoOfferNotice />}
        {(offer.brand || offer.model) && (
          <p className="offer-identity">
            {[offer.brand, offer.model].filter(Boolean).join(' · ')}
          </p>
        )}
        <div className="offer-price">
          {formatOfferPrice(offer.price_minor, offer.currency)}
        </div>
        <ProviderCartAddAction
          offer={offer}
          providers={providers}
          providersLoading={providersLoading}
          busyOfferId={providerAddBusyOfferId}
          notice={providerAddNotice}
          syncRequired={providerCartSyncRequired}
          onAdd={onAddToProviderCart}
          onRetrySync={onRetryProviderCartSync}
          onSignIn={onSignIn}
        />
        {onToggleComparison && (
          <button
            className={`comparison-select-button${isSelected ? ' selected' : ''}`}
            type="button"
            aria-pressed={isSelected}
            disabled={!isSelected && selectionDisabled}
            onClick={() => onToggleComparison(offer, providerName, discoveryPartial)}
          >
            {isSelected ? 'Selected for comparison' : 'Add to compare'}
          </button>
        )}
        <p className="offer-availability">
          {isDemoOffer
            ? `Sample availability: ${offer.availability || 'Unknown'} (not live).`
            : `Availability: ${offer.availability || 'Not provided'}`}
        </p>
        {!isDemoOffer && productUrl && (
          <a
            className="offer-link"
            href={productUrl}
            target="_blank"
            rel="noopener noreferrer"
          >
            View provider listing
          </a>
        )}
        {ranking && (
          <div className="offer-ranking">
            {Number.isFinite(ranking.relevance_score) && (
              <strong>
                Uncalibrated relevance: {ranking.relevance_score.toFixed(2)}
              </strong>
            )}
            {Array.isArray(ranking.matched_attributes) &&
              ranking.matched_attributes.length > 0 && (
                <span>
                  Matched catalog attributes:{' '}
                  {ranking.matched_attributes
                    .filter((attribute) => typeof attribute === 'string')
                    .map((attribute) => attribute.replaceAll('_', ' '))
                    .join(', ')}
                </span>
              )}
            {ranking.match_basis === 'no_catalog_text_evidence' && (
              <span>No matching catalog text evidence.</span>
            )}
            {ranking.match_basis === 'catalog_text' && (
              <span>Ranked against available catalog text attributes.</span>
            )}
          </div>
        )}
      </div>
    </article>
  )
}

function App() {
  const [activeNav, setActiveNav] = useState('Dashboard')
  const [cartSection, setCartSection] = useState('local')
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('All')
  const [cart, setCart] = useState([])
  const [cartSubtotal, setCartSubtotal] = useState(0)
  const [cartCurrency, setCartCurrency] = useState(null)
  const [authStatus, setAuthStatus] = useState('loading')
  const [signOutPending, setSignOutPending] = useState(false)
  const [signOutError, setSignOutError] = useState('')
  const [cartLoading, setCartLoading] = useState(true)
  const [cartOperation, setCartOperation] = useState('')
  const [cartError, setCartError] = useState('')
  const [orderSubmitting, setOrderSubmitting] = useState(false)
  const [orderSuccess, setOrderSuccess] = useState('')
  const [orders, setOrders] = useState([])
  const [ordersLoading, setOrdersLoading] = useState(false)
  const [ordersError, setOrdersError] = useState('')
  const [ordersReload, setOrdersReload] = useState(0)
  const [chatInput, setChatInput] = useState('')
  const [messages, setMessages] = useState([
    { from: 'assistant', text: 'Hello! I can help you discover products and compare deals. What are you shopping for today?' },
  ])
  const [activeCartConfirmationToken, setActiveCartConfirmationToken] = useState(null)

  const [isChatLoading, setIsChatLoading] = useState(false)
  const [chatError, setChatError] = useState('')
  const [isVoiceListening, setIsVoiceListening] = useState(false)
  const [voiceInputError, setVoiceInputError] = useState('')
  const [apiProducts, setApiProducts] = useState([])
  const [apiCategories, setApiCategories] = useState([])
  const [productsLoading, setProductsLoading] = useState(true)
  const [productsError, setProductsError] = useState('')
  const [providers, setProviders] = useState([])
  const [providersLoading, setProvidersLoading] = useState(true)
  const [providersError, setProvidersError] = useState('')
  const [providerAddBusyOfferId, setProviderAddBusyOfferId] = useState(null)
  const [providerAddNotice, setProviderAddNotice] = useState(null)
  const [providerCartSyncRequired, setProviderCartSyncRequired] = useState(false)
  const providerAddInFlightRef = useRef(false)
  const [offerQuery, setOfferQuery] = useState('')
  const [offerSearchMode, setOfferSearchMode] = useState('offers')
  const [offerProviderKey, setOfferProviderKey] = useState('')
  const [offerMaxPrice, setOfferMaxPrice] = useState('')
  const [offerOrganic, setOfferOrganic] = useState('')
  const [offers, setOffers] = useState([])
  const [offersLoading, setOffersLoading] = useState(false)
  const [offersSearched, setOffersSearched] = useState(false)
  const [offersPartial, setOffersPartial] = useState(false)
  const [offersError, setOffersError] = useState('')
  const [smartSearchResult, setSmartSearchResult] = useState(null)
  const [comparisonSelection, setComparisonSelection] = useState([])
  const [comparisonBudgetAmount, setComparisonBudgetAmount] = useState('')
  const [comparisonBudgetCurrency, setComparisonBudgetCurrency] = useState('')
  const [comparisonMustHaves, setComparisonMustHaves] = useState([])
  const [comparisonAttribute, setComparisonAttribute] = useState('is_organic')
  const [comparisonExpectedValue, setComparisonExpectedValue] = useState('true')
  const [comparisonLoading, setComparisonLoading] = useState(false)
  const [comparisonError, setComparisonError] = useState('')
  const [comparisonInvalidSelection, setComparisonInvalidSelection] = useState(false)
  const [comparisonAuthRequired, setComparisonAuthRequired] = useState(false)
  const [comparisonResult, setComparisonResult] = useState(null)
  const [selectedImage, setSelectedImage] = useState(null)
  const [imagePreviewUrl, setImagePreviewUrl] = useState('')
  const [isCapturedImage, setIsCapturedImage] = useState(false)
  const [isCameraOpen, setIsCameraOpen] = useState(false)
  const [cameraError, setCameraError] = useState('')
  const [cameraDevices, setCameraDevices] = useState([])
  const [activeCameraId, setActiveCameraId] = useState('')
  const [imageProviderKey, setImageProviderKey] = useState('')
  const [imageMaxPrice, setImageMaxPrice] = useState('')
  const [imageCurrency, setImageCurrency] = useState('')
  const [imageOrganic, setImageOrganic] = useState('')
  const [imageSearchLoading, setImageSearchLoading] = useState(false)
  const [imageSearchError, setImageSearchError] = useState('')
  const [imageSearchRetrySeconds, setImageSearchRetrySeconds] = useState(0)
  const [imageSearchRetryUntil, setImageSearchRetryUntil] = useState(0)
  const [imageSearchResult, setImageSearchResult] = useState(null)
  const [imageSearchSearched, setImageSearchSearched] = useState(false)
  const imageFileInputRef = useRef(null)
  const imagePreviewUrlRef = useRef('')
  const cameraVideoRef = useRef(null)
  const cameraStreamRef = useRef(null)
  const cameraRequestIdRef = useRef(0)
  const imageSearchInFlightRef = useRef(false)
  const comparisonInFlightRef = useRef(false)
  const voiceRecognitionRef = useRef(null)
  const voiceInputBaseRef = useRef('')
  const productSearchInputRef = useRef(null)
  const pendingProductSearchFocusRef = useRef(false)
  const offerSearchPanelRef = useRef(null)
  const pendingOfferSearchScrollRef = useRef(false)
  const stopCamera = useCallback((updateUi = true) => {
    cameraRequestIdRef.current += 1
    cameraStreamRef.current?.getTracks().forEach((track) => track.stop())
    cameraStreamRef.current = null
    if (cameraVideoRef.current) cameraVideoRef.current.srcObject = null
    if (updateUi) setIsCameraOpen(false)
  }, [])
  const imageRankings = useMemo(
    () =>
      new Map(
        (Array.isArray(imageSearchResult?.rankings)
          ? imageSearchResult.rankings
          : []
        )
          .filter(
            (ranking) =>
              ranking && typeof ranking.offer_id === 'string',
          )
          .map((ranking) => [ranking.offer_id, ranking]),
      ),
    [imageSearchResult],
  )

  useEffect(() => {
    return () => {
      stopCamera(false)
      if (imagePreviewUrlRef.current) {
        URL.revokeObjectURL(imagePreviewUrlRef.current)
        imagePreviewUrlRef.current = ''
      }
      const activeRecognition = voiceRecognitionRef.current
      voiceRecognitionRef.current = null
      activeRecognition?.abort()
    }
  }, [stopCamera])

  useLayoutEffect(() => {
    return () => {
      if (activeNav === 'Shop by Image') stopCamera(false)
    }
  }, [activeNav, stopCamera])

  useEffect(() => {
    if (!isCameraOpen || !cameraVideoRef.current || !cameraStreamRef.current) return
    cameraVideoRef.current.srcObject = cameraStreamRef.current
    cameraVideoRef.current.play().catch(() => {
      setCameraError('Camera preview could not start. Check your browser permissions and try again.')
    })
  }, [isCameraOpen])

  useEffect(() => {
    if (!imageSearchRetryUntil) return undefined

    const updateCountdown = () => {
      const secondsRemaining = Math.max(
        0,
        Math.ceil((imageSearchRetryUntil - Date.now()) / 1000),
      )
      setImageSearchRetrySeconds(secondsRemaining)
      if (secondsRemaining === 0) {
        setImageSearchRetryUntil(0)
        setImageSearchError('')
      }
    }

    updateCountdown()
    const intervalId = window.setInterval(updateCountdown, 1000)
    return () => window.clearInterval(intervalId)
  }, [imageSearchRetryUntil])

  useLayoutEffect(() => {
    if (activeNav !== 'Products' || !pendingOfferSearchScrollRef.current) return

    pendingOfferSearchScrollRef.current = false
    offerSearchPanelRef.current?.scrollIntoView({ block: 'start' })
  }, [activeNav])

  useLayoutEffect(() => {
    if (activeNav !== 'Products' || !pendingProductSearchFocusRef.current) return

    pendingProductSearchFocusRef.current = false
    productSearchInputRef.current?.focus()
    productSearchInputRef.current?.scrollIntoView({ block: 'center' })
  }, [activeNav])

  const refreshCart = useCallback(async (silent = false) => {
    if (!silent) setCartLoading(true)
    setCartError('')

    try {
      const response = await fetch('/api/cart')
      const data = await readApiResponse(response, 'Cart request')

      if (
        !Array.isArray(data.items) ||
        (typeof data.subtotal !== 'number' && data.subtotal !== null)
      ) {
        throw new Error('The cart API returned an unexpected response.')
      }

      setCart(data.items)
      setCartSubtotal(data.subtotal)
      setCartCurrency(data.currency ?? null)
      return true
    } catch (error) {
      if (error.status === 401) {
        setAuthStatus('unauthenticated')
        setCart([])
        setCartSubtotal(0)
        setCartCurrency(null)
        setCartError('')
      } else {
        setCartError(
          error instanceof TypeError
            ? 'Could not reach the cart service. Check that the FastAPI backend is running.'
            : error instanceof Error
              ? error.message
              : 'Could not load your cart.',
        )
      }
      return false
    } finally {
      if (!silent) setCartLoading(false)
    }
  }, [])

  const refreshAuthentication = useCallback(async () => {
    try {
      const response = await fetch('/auth/me')
      const data = await readApiResponse(response, 'Authentication request')
      if (!data.authenticated) {
        setAuthStatus('unauthenticated')
        setCart([])
        setCartSubtotal(0)
        setCartCurrency(null)
        setCartLoading(false)
        return
      }

      setAuthStatus('authenticated')
      setComparisonAuthRequired(false)
      setComparisonError((current) =>
        current === 'Sign in with Google to compare products.' ? '' : current,
      )
      await refreshCart()
    } catch (error) {
      setAuthStatus('error')
      setCartLoading(false)
      setCartError(
        error instanceof TypeError
          ? 'Could not verify your sign-in. Check that the FastAPI backend is running.'
          : error instanceof Error
            ? error.message
            : 'Could not verify your sign-in.',
      )
    }
  }, [refreshCart])

  useEffect(() => {
    let cancelled = false

    async function checkAuthentication() {
      try {
        const response = await fetch('/auth/me')
        const data = await readApiResponse(response, 'Authentication request')
        if (cancelled) return

        if (!data.authenticated) {
          setAuthStatus('unauthenticated')
          setCart([])
          setCartSubtotal(0)
          setCartCurrency(null)
          setCartLoading(false)
          return
        }

        setAuthStatus('authenticated')
        await refreshCart()
      } catch (error) {
        if (cancelled) return
        setAuthStatus('error')
        setCartLoading(false)
        setCartError(
          error instanceof TypeError
            ? 'Could not verify your sign-in. Check that the FastAPI backend is running.'
            : error instanceof Error
              ? error.message
              : 'Could not verify your sign-in.',
        )
      }
    }

    checkAuthentication()
    window.addEventListener('focus', refreshAuthentication)
    return () => {
      cancelled = true
      window.removeEventListener('focus', refreshAuthentication)
    }
  }, [refreshAuthentication, refreshCart])

  useEffect(() => {
    if (activeNav !== 'My Orders' || authStatus !== 'authenticated') return undefined

    const controller = new AbortController()
    let cancelled = false

    async function loadOrders() {
      try {
        const response = await fetch('/api/orders', {
          credentials: 'same-origin',
          signal: controller.signal,
        })
        const data = await readApiResponse(response, 'Order history request')
        if (
          !Array.isArray(data.orders) ||
          data.orders.some(
            (order) =>
              !order ||
              typeof order.order_id !== 'string' ||
              typeof order.ordered_at !== 'string' ||
              typeof order.status !== 'string' ||
              (order.currency !== null && typeof order.currency !== 'string') ||
              (order.total_minor !== null &&
                !Number.isSafeInteger(order.total_minor)) ||
              !Array.isArray(order.items) ||
              order.items.some(
                (item) =>
                  !item ||
                  typeof item.product_name !== 'string' ||
                  !Number.isSafeInteger(item.quantity) ||
                  item.quantity < 1 ||
                  (item.unit_price !== null &&
                    typeof item.unit_price !== 'number') ||
                  (item.currency !== null &&
                    typeof item.currency !== 'string'),
              ),
          )
        ) {
          throw new Error('The order history API returned an unexpected response.')
        }
        if (!cancelled) setOrders(data.orders)
      } catch (error) {
        if (cancelled || error.name === 'AbortError') return
        if (error.status === 401) {
          setAuthStatus('unauthenticated')
          setOrders([])
          return
        }
        setOrdersError(
          error instanceof TypeError
            ? 'Could not reach the order service. Check that the FastAPI backend is running.'
            : error instanceof Error
              ? error.message
              : 'Could not load your order history.',
        )
      } finally {
        if (!cancelled) setOrdersLoading(false)
      }
    }

    loadOrders()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [activeNav, authStatus, ordersReload])

  useEffect(() => {
    let cancelled = false

    async function loadCatalog() {
      setProductsLoading(true)
      setProductsError('')

      try {
        const [productsResponse, categoriesResponse] = await Promise.all([
          fetch('/api/products'),
          fetch('/api/categories'),
        ])

        const [productsData, categoriesData] = await Promise.all([
          readApiResponse(productsResponse, 'Product request'),
          readApiResponse(categoriesResponse, 'Category request'),
        ])

        if (
          !Array.isArray(productsData.products) ||
          !Array.isArray(categoriesData.categories)
        ) {
          throw new Error('The catalog API returned an unexpected response.')
        }

        if (!cancelled) {
          setApiProducts(productsData.products)
          setApiCategories(categoriesData.categories)
        }
      } catch (error) {
        if (!cancelled) {
          const reason =
            error instanceof TypeError
              ? 'Check that the FastAPI backend is running.'
              : error instanceof Error
                ? error.message
                : 'Please try again.'
          setProductsError(`Could not load the product catalog. ${reason}`)
        }
      } finally {
        if (!cancelled) setProductsLoading(false)
      }
    }

    loadCatalog()

    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    let cancelled = false

    async function loadProviders() {
      setProvidersLoading(true)
      setProvidersError('')

      try {
        const response = await fetch('/api/providers')
        const data = await readApiResponse(response, 'Provider request')

        if (
          !Array.isArray(data.providers) ||
          !data.providers.every(
            (provider) =>
              provider &&
              typeof provider.provider_key === 'string' &&
              typeof provider.display_name === 'string',
          )
        ) {
          throw new Error('The provider API returned an unexpected response.')
        }

        if (!cancelled) {
          setProviders(
            data.providers.map((provider) => ({
              ...provider,
              supports_external_cart:
                provider.supports_external_cart === true,
            })),
          )
        }
      } catch (error) {
        if (!cancelled) {
          setProvidersError(
            error instanceof TypeError
              ? 'Could not reach provider discovery. Check that the FastAPI backend is running.'
              : error instanceof Error
                ? error.message
                : 'Could not load providers.',
          )
        }
      } finally {
        if (!cancelled) setProvidersLoading(false)
      }
    }

    loadProviders()

    return () => {
      cancelled = true
    }
  }, [])

  const categoryOptions = [
    'All',
    ...apiCategories.map((item) => item.category).filter(Boolean),
  ]

  const filteredProducts = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase()

    return apiProducts.filter((product) => {
      const searchableText = [
        product.name,
        product.description,
        product.category,
      ]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()

      const matchesQuery = searchableText.includes(normalizedQuery)
      const matchesCategory =
        category === 'All' ||
        product.category?.toLowerCase() === category.toLowerCase()

      return matchesQuery && matchesCategory
    })
  }, [apiProducts, query, category])

  async function searchOffers(event) {
    event.preventDefault()
    setOffersSearched(true)
    setOffersPartial(false)
    setOffersError('')
    setSmartSearchResult(null)

    if (offerSearchMode === 'smart') {
      const query = offerQuery.trim()
      if (!query) {
        setOffers([])
        setOffersError('Enter a natural-language query to use Smart Search.')
        return
      }

      setOffersLoading(true)
      try {
        const response = await fetch('/api/search/smart', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query }),
        })
        const data = await readApiResponse(response, 'Smart Search')

        if (!isSmartSearchResponse(data)) {
          throw new Error('The Smart Search API returned an unexpected response.')
        }

        setOffers(data.offers)
        setOffersPartial(data.partial)
        setSmartSearchResult(data)
      } catch (error) {
        setOffers([])
        setOffersPartial(false)
        setOffersError(
          error instanceof TypeError
            ? 'Could not reach Smart Search. Check that the FastAPI backend is running.'
            : error.status === 503
              ? 'Smart Search is temporarily unavailable. Try again later.'
              : error instanceof Error
                ? error.message
                : 'Smart Search failed. Please try again.',
        )
      } finally {
        setOffersLoading(false)
      }
      return
    }

    const parameters = new URLSearchParams()
    const normalizedQuery = offerQuery.trim()
    if (normalizedQuery) parameters.set('query', normalizedQuery)
    if (offerProviderKey) parameters.set('provider_key', offerProviderKey)
    if (offerOrganic) parameters.set('is_organic', offerOrganic)

    if (offerMaxPrice.trim()) {
      const maxPrice = Number(offerMaxPrice)
      if (!Number.isFinite(maxPrice) || maxPrice < 0) {
        setOffers([])
        setOffersError('Maximum price must be a non-negative INR amount.')
        return
      }
      parameters.set('max_price', offerMaxPrice.trim())
      parameters.set('currency', 'INR')
    }

    setOffersLoading(true)
    try {
      const response = await fetch(
        `/api/offers/search?${parameters.toString()}`,
      )
      const data = await readApiResponse(response, 'Offer search')

      if (
        !Array.isArray(data.offers) ||
        !data.offers.every((offer) => offer && typeof offer === 'object')
      ) {
        throw new Error('The offer search API returned an unexpected response.')
      }

      setOffers(data.offers)
      setOffersPartial(data.partial === true)
    } catch (error) {
      setOffers([])
      setOffersPartial(false)
      setOffersError(
        error instanceof TypeError
          ? 'Could not reach offer search. Check that the FastAPI backend is running.'
          : error.status === 503
            ? 'Offer providers are temporarily unavailable. Try again later.'
            : error instanceof Error
              ? error.message
              : 'Offer search failed. Please try again.',
      )
    } finally {
      setOffersLoading(false)
    }
  }

  function resetComparisonResult() {
    setComparisonResult(null)
    setComparisonError('')
    setComparisonInvalidSelection(false)
    setComparisonAuthRequired(false)
  }

  function toggleComparisonSelection(offer, providerName, discoveryPartial = false) {
    const reference = {
      provider_key: offer?.provider_key,
      provider_product_id: offer?.provider_product_id,
    }
    const referenceKey = comparisonReferenceKey(reference)
    if (!referenceKey) {
      setComparisonError('This offer has no verified product reference.')
      return
    }

    const isSelected = comparisonSelection.some(
      (item) => comparisonReferenceKey(item.reference) === referenceKey,
    )
    if (isSelected) {
      setActiveCartConfirmationToken(null)
      setComparisonSelection((current) =>
        current.filter(
          (item) => comparisonReferenceKey(item.reference) !== referenceKey,
        ),
      )
      resetComparisonResult()
      return
    }
    if (comparisonSelection.length >= 5) {
      setComparisonError('You can compare up to five products at a time.')
      return
    }

    setActiveCartConfirmationToken(null)
    setComparisonSelection((current) => [
      ...current,
      { reference, offer, providerName, discoveryPartial },
    ])
    resetComparisonResult()
  }

  function addMustHaveCriterion(event) {
    event.preventDefault()
    if (comparisonMustHaves.length >= 8) {
      setComparisonError('You can add up to eight must-have requirements.')
      return
    }

    let expectedValue = comparisonExpectedValue.trim()
    if (!expectedValue) {
      setComparisonError('Enter an expected value for this requirement.')
      return
    }
    if (comparisonAttribute === 'is_organic') {
      if (!['true', 'false'].includes(expectedValue)) {
        setComparisonError('Choose yes or no for the organic requirement.')
        return
      }
      expectedValue = expectedValue === 'true'
    } else if (comparisonAttribute === 'minimum_rating') {
      expectedValue = Number(expectedValue)
      if (!Number.isFinite(expectedValue) || expectedValue < 0 || expectedValue > 5) {
        setComparisonError('Minimum rating must be between 0 and 5.')
        return
      }
    } else if (expectedValue.length > 120) {
      setComparisonError('Requirement values must be 120 characters or fewer.')
      return
    }

    setComparisonMustHaves((current) => [
      ...current,
      { attribute: comparisonAttribute, expected_value: expectedValue },
    ])
    setComparisonExpectedValue(
      comparisonAttribute === 'is_organic' ? 'true' : '',
    )
    resetComparisonResult()
  }

  async function submitComparison(event) {
    event.preventDefault()
    if (comparisonInFlightRef.current) return

    if (comparisonSelection.length < 2 || comparisonSelection.length > 5) {
      setComparisonError('Select between two and five products to compare.')
      setComparisonInvalidSelection(true)
      return
    }
    if (authStatus === 'loading') {
      setComparisonError('Please wait while your sign-in is checked.')
      return
    }
    if (authStatus !== 'authenticated') {
      setComparisonAuthRequired(true)
      setComparisonError('')
      return
    }
    if (
      comparisonBudgetAmount.trim() &&
      (!Number.isFinite(Number(comparisonBudgetAmount)) ||
        Number(comparisonBudgetAmount) < 0)
    ) {
      setComparisonError('Enter a non-negative budget amount.')
      return
    }
    if (comparisonBudgetAmount.trim() && !comparisonBudgetCurrency) {
      setComparisonError('Choose a currency for your budget.')
      return
    }

    const requestBody = {
      candidates: comparisonSelection.map((item) => item.reference),
      must_have: comparisonMustHaves,
    }
    if (comparisonBudgetAmount.trim()) {
      requestBody.budget = {
        amount: comparisonBudgetAmount.trim(),
        currency: comparisonBudgetCurrency,
      }
    }

    comparisonInFlightRef.current = true
    setComparisonLoading(true)
    setComparisonError('')
    setComparisonInvalidSelection(false)
    setComparisonAuthRequired(false)
    setComparisonResult(null)

    try {
      const response = await fetch('/api/offers/compare', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(requestBody),
      })
      const data = await readApiResponse(response, 'Product comparison')
      const references = requestBody.candidates
      if (!isComparisonResponse(data, references)) {
        throw new Error('The comparison service returned an unexpected response.')
      }
      setComparisonResult(data)
    } catch (error) {
      if (error.status === 401) {
        setAuthStatus('unauthenticated')
        setComparisonAuthRequired(true)
        setComparisonError('Sign in with Google to compare products.')
      } else if (error.status === 403) {
        setComparisonError('This frontend origin is not authorized to compare products.')
      } else if (error.status === 404) {
        setComparisonInvalidSelection(true)
        setComparisonError(
          'A selected product or provider is no longer available. Remove it and select current discovery results.',
        )
      } else if (error.status === 422) {
        setComparisonInvalidSelection(true)
        setComparisonError(
          'The selection or comparison requirements are invalid. Review them and try again.',
        )
      } else if (error.status === 503) {
        setComparisonError('Verified product information is temporarily unavailable.')
      } else if (error instanceof TypeError) {
        setComparisonError(
          'Could not reach product comparison. Check that the FastAPI backend is running.',
        )
      } else {
        setComparisonError(
          error instanceof Error
            ? error.message
            : 'Product comparison failed. Please try again.',
        )
      }
    } finally {
      comparisonInFlightRef.current = false
      setComparisonLoading(false)
    }
  }

    function releaseImagePreview() {
      if (imagePreviewUrlRef.current) {
        URL.revokeObjectURL(imagePreviewUrlRef.current)
        imagePreviewUrlRef.current = ''
      }
      setImagePreviewUrl('')
    }

    function selectImageFile(file, captured = false) {
      if (!file) return

      const extension = file.name
        .slice(file.name.lastIndexOf('.'))
        .toLowerCase()
      const supportedTypes = IMAGE_FILE_TYPES[extension]
      if (!supportedTypes || !supportedTypes.includes(file.type.toLowerCase())) {
        setImageSearchError('Choose a valid JPEG, PNG, or WebP image.')
        return
      }
      if (file.size > MAX_IMAGE_FILE_BYTES) {
        setImageSearchError('The image must be no larger than 10 MiB.')
        return
      }

      releaseImagePreview()
      const previewUrl = URL.createObjectURL(file)
      imagePreviewUrlRef.current = previewUrl
      setImagePreviewUrl(previewUrl)
      setSelectedImage(file)
      setIsCapturedImage(captured)
      setImageSearchError('')
      setImageSearchResult(null)
      setImageSearchSearched(false)
    }

    function handleImageSelection(event) {
      selectImageFile(event.target.files?.[0])
      event.target.value = ''
    }

    async function startCamera(deviceId = '') {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
        setCameraError(
          'Camera access is unavailable in this browser or connection. Use a secure connection or upload an image instead.',
        )
        return
      }

      const requestId = cameraRequestIdRef.current + 1
      cameraRequestIdRef.current = requestId
      cameraStreamRef.current?.getTracks().forEach((track) => track.stop())
      cameraStreamRef.current = null
      setCameraDevices([])
      setCameraError('')
      setIsCameraOpen(true)

      let stream
      try {
        const videoConstraints = deviceId
          ? { deviceId: { exact: deviceId } }
          : { facingMode: { ideal: 'environment' } }
        try {
          stream = await navigator.mediaDevices.getUserMedia({
            video: videoConstraints,
            audio: false,
          })
        } catch (error) {
          if (
            deviceId ||
            error.name === 'NotAllowedError' ||
            error.name === 'SecurityError'
          ) {
            throw error
          }
          stream = await navigator.mediaDevices.getUserMedia({
            video: true,
            audio: false,
          })
        }

        if (cameraRequestIdRef.current !== requestId) {
          stream.getTracks().forEach((track) => track.stop())
          return
        }

        cameraStreamRef.current = stream
        if (cameraVideoRef.current) {
          cameraVideoRef.current.srcObject = stream
          cameraVideoRef.current.play().catch(() => {
            setCameraError(
              'Camera preview could not start. Check your browser permissions and try again.',
            )
          })
        }
        const [devices, track] = await Promise.all([
          navigator.mediaDevices.enumerateDevices(),
          Promise.resolve(stream.getVideoTracks()[0]),
        ])
        if (cameraRequestIdRef.current !== requestId) return
        setCameraDevices(devices.filter((device) => device.kind === 'videoinput'))
        setActiveCameraId(track?.getSettings().deviceId || deviceId)
      } catch (error) {
        if (cameraRequestIdRef.current !== requestId) return
        cameraStreamRef.current?.getTracks().forEach((track) => track.stop())
        cameraStreamRef.current = null
        if (cameraVideoRef.current) cameraVideoRef.current.srcObject = null
        setIsCameraOpen(false)
        if (error.name === 'NotAllowedError' || error.name === 'SecurityError') {
          setCameraError('Camera permission was denied. Allow camera access or upload an image instead.')
        } else if (error.name === 'NotFoundError' || error.name === 'DevicesNotFoundError') {
          setCameraError('No camera was found. Upload an image instead.')
        } else if (error.name === 'NotReadableError' || error.name === 'TrackStartError') {
          setCameraError('The camera is unavailable or already in use. Upload an image or try again.')
        } else {
          setCameraError('Could not open the camera. Upload an image or try again.')
        }
      }
    }

    function capturePhoto() {
      const video = cameraVideoRef.current
      if (!video?.videoWidth || !video?.videoHeight) {
        setCameraError('The camera is not ready yet. Please wait and try again.')
        return
      }

      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth
      canvas.height = video.videoHeight
      const context = canvas.getContext('2d')
      if (!context) {
        setCameraError('Photo capture is unavailable. Please try again or upload an image.')
        return
      }
      context.drawImage(video, 0, 0, canvas.width, canvas.height)
      const captureRequestId = cameraRequestIdRef.current
      canvas.toBlob((blob) => {
        if (cameraRequestIdRef.current !== captureRequestId) return
        if (!blob) {
          setCameraError('Could not capture the photo. Please try again.')
          return
        }
        const file = new File([blob], `camera-photo-${Date.now()}.jpg`, {
          type: 'image/jpeg',
        })
        selectImageFile(file, true)
        stopCamera()
      }, 'image/jpeg', 0.92)
    }

    function clearImageSelection() {
      releaseImagePreview()
      setSelectedImage(null)
      setIsCapturedImage(false)
      setImageSearchError('')
      setImageSearchResult(null)
      setImageSearchSearched(false)
      if (imageFileInputRef.current) imageFileInputRef.current.value = ''
    }

    async function searchByImage(event) {
      event.preventDefault()
      if (imageSearchInFlightRef.current || imageSearchRetrySeconds > 0) return
      if (!selectedImage) {
        setImageSearchError('Choose an image before starting the search.')
        return
      }

      const normalizedMaxPrice = imageMaxPrice.trim()
      if (normalizedMaxPrice) {
        const maxPrice = Number(normalizedMaxPrice)
        if (!Number.isFinite(maxPrice) || maxPrice < 0) {
          setImageSearchError('Maximum price must be a non-negative amount.')
          return
        }
        if (!imageCurrency) {
          setImageSearchError('Choose a currency for the maximum price filter.')
          return
        }
      }

      const formData = new FormData()
      formData.append('image', selectedImage)
      if (imageProviderKey) formData.append('provider_key', imageProviderKey)
      if (normalizedMaxPrice) {
        formData.append('max_price', normalizedMaxPrice)
        formData.append('currency', imageCurrency)
      }
      if (imageOrganic) formData.append('is_organic', imageOrganic)

      imageSearchInFlightRef.current = true
      setImageSearchLoading(true)
      setImageSearchError('')
      setImageSearchRetrySeconds(0)
      setImageSearchRetryUntil(0)
      setImageSearchResult(null)
      setImageSearchSearched(false)

      try {
        const response = await fetch('/api/offers/search-by-image', {
          method: 'POST',
          credentials: 'same-origin',
          body: formData,
        })
        const data = await readApiResponse(response, 'Image search')
        if (
          !Array.isArray(data.offers) ||
          !Array.isArray(data.providers) ||
          !Array.isArray(data.groups) ||
          !Array.isArray(data.price_summaries) ||
          !Array.isArray(data.rankings) ||
          !data.visual_analysis ||
          typeof data.visual_analysis !== 'object'
        ) {
          throw new Error('The image search service returned an unexpected response.')
        }
        setImageSearchResult(data)
        setImageSearchSearched(true)
      } catch (error) {
        if (error.status === 401) {
          setImageSearchError('Your sign-in is required to search by image.')
          await refreshAuthentication()
        } else if (error instanceof TypeError) {
          setImageSearchError(
            'Could not reach visual search. Check that the FastAPI backend is running.',
          )
        } else if (error.status === 400) {
          setImageSearchError('Check the selected image and search filters.')
        } else if (error.status === 403) {
          setImageSearchError(
            'This frontend origin is not authorized for image search.',
          )
        } else if (error.status === 404) {
          setImageSearchError('The selected provider is not available.')
        } else if (error.status === 413) {
          setImageSearchError('The image must be no larger than 10 MiB.')
        } else if (error.status === 429) {
          const retryAfterSeconds = parseRetryAfterSeconds(error.retryAfter)
          setImageSearchError(
            retryAfterSeconds === null
              ? "You've reached your image search limit. Please try again later."
              : "You've reached your image search limit.",
          )
          if (retryAfterSeconds !== null) {
            setImageSearchRetrySeconds(retryAfterSeconds)
            setImageSearchRetryUntil(Date.now() + retryAfterSeconds * 1000)
          }
        } else if (error.status === 502) {
          setImageSearchError('Image analysis or provider search failed.')
        } else if (error.status === 503) {
          setImageSearchError(
            'Visual search is temporarily unavailable. Try again later.',
          )
        } else if (error.status === 504) {
          setImageSearchError('Image search timed out. Try again.')
        } else {
          setImageSearchError('Image search failed. Please try again.')
        }
      } finally {
        imageSearchInFlightRef.current = false
        setImageSearchLoading(false)
      }
    }

  const cartCount = cart.reduce((total, item) => total + item.quantity, 0)
  const hasDemoComparisonSelection = comparisonSelection.some(
    (item) => item.reference.provider_key === 'demo_catalog',
  )
  const cartSubtotalText =
    cart.length === 0
      ? '0.00'
      : cartSubtotal === null
        ? 'Currency unverified or mixed'
        : formatMajorPrice(cartSubtotal, cartCurrency)

  function navigateTo(navItem) {
    if (navItem === 'My Orders') {
      setOrdersLoading(true)
      setOrdersError('')
      setOrdersReload((current) => current + 1)
    } else if (activeNav === 'My Orders') {
      setOrdersLoading(false)
    }
    setActiveNav(navItem)
  }

  async function performCartOperation(operation, url, options = {}) {
    if (authStatus !== 'authenticated') {
      setCartError('Sign in with Google to manage your cart.')
      setActiveNav('My Cart')
      return
    }

    setCartOperation(operation)
    setCartError('')
    setOrderSuccess('')
    try {
      const response = await fetch(url, options)
      await readApiResponse(response, 'Cart update')
      await refreshCart(true)
    } catch (error) {
      if (error.status === 401) {
        setAuthStatus('unauthenticated')
        setCart([])
        setCartSubtotal(0)
        setCartCurrency(null)
      }
      setCartError(
        error instanceof TypeError
          ? 'Could not reach the cart service. Check that the FastAPI backend is running.'
          : error instanceof Error
            ? error.message
            : 'Could not update your cart.',
      )
    } finally {
      setCartOperation('')
    }
  }

  function addToCart(product) {
    return performCartOperation(
      `add-${product.id}`,
      '/api/cart/items',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ product_id: product.id, quantity: 1 }),
      },
    )
  }

  function changeCartQuantity(product, quantity) {
    if (quantity < 1) {
      return performCartOperation(
        `remove-${product.product_id}`,
        `/api/cart/items/${product.product_id}`,
        { method: 'DELETE' },
      )
    }
    return performCartOperation(
      `update-${product.product_id}`,
      `/api/cart/items/${product.product_id}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quantity }),
      },
    )
  }

  function removeCartItem(productId) {
    return performCartOperation(
      `remove-${productId}`,
      `/api/cart/items/${productId}`,
      { method: 'DELETE' },
    )
  }

  function clearCart() {
    return performCartOperation('clear', '/api/cart', { method: 'DELETE' })
  }

  async function placeDemoOrder() {
    if (authStatus !== 'authenticated') {
      setCartError('Sign in with Google to place a demo order.')
      return
    }
    if (cart.length === 0 || cartOperation || orderSubmitting) return

    setOrderSubmitting(true)
    setCartError('')
    setOrderSuccess('')
    try {
      const response = await fetch('/api/orders', {
        method: 'POST',
        credentials: 'same-origin',
      })
      const data = await readApiResponse(response, 'Demo order')
      if (
        !data.order ||
        typeof data.order.order_id !== 'string' ||
        !data.order.order_id
      ) {
        throw new Error('The order API returned an unexpected response.')
      }
      setCart([])
      setCartSubtotal(0)
      setCartCurrency(null)
      setOrderSuccess(
        `Demo order ${data.order.order_id} was saved. No payment was collected and no purchase was made from a retailer.`,
      )
    } catch (error) {
      if (error.status === 401) {
        setAuthStatus('unauthenticated')
      }
      setCartError(
        error instanceof TypeError
          ? 'Could not reach the order service. Check that the FastAPI backend is running.'
          : error instanceof Error
            ? error.message
            : 'Could not place the demo order.',
      )
    } finally {
      setOrderSubmitting(false)
    }
  }

  async function addToProviderCart(offerId) {
    if (
      providerAddInFlightRef.current ||
      providerCartSyncRequired ||
      typeof offerId !== 'string' ||
      !offerId.trim()
    ) {
      return
    }

    providerAddInFlightRef.current = true
    setProviderAddBusyOfferId(offerId)
    setProviderAddNotice(null)
    let mutationError = null

    try {
      const response = await fetch('/api/cart/external/items', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ offer_id: offerId, quantity: 1 }),
      })
      await readApiResponse(response, 'Provider cart add')
    } catch (error) {
      mutationError = error
    }

    try {
      const snapshot = await fetchExternalCartSnapshot()
      setProviderCartSyncRequired(false)
      const item = snapshot.items.find((entry) => entry.offer_id === offerId)

      if (mutationError) {
        const ambiguous =
          mutationError instanceof TypeError ||
          !Number.isInteger(mutationError.status)
        const message = ambiguous
          ? item
            ? `The add result was uncertain. Provider Offers was reloaded; this offer is present at quantity ${item.quantity}. Review it before adding again.`
            : 'The add result was uncertain. Provider Offers was reloaded and this offer is not present; you can retry if needed.'
          : `${providerCartErrorMessage(mutationError)} Provider Offers was reloaded${
              item ? `; the current quantity is ${item.quantity}` : ''
            }.`
        setProviderAddNotice({
          offerId,
          isError: true,
          status: mutationError.status,
          message,
        })
      } else if (item) {
        setProviderAddNotice({
          offerId,
          isError: false,
          message: `Added to Provider Offers. The server cart now shows quantity ${item.quantity}.`,
        })
      } else {
        setProviderAddNotice({
          offerId,
          isError: true,
          message:
            'The add request succeeded, but the offer was not found in the reloaded cart. Verify the cart before retrying.',
        })
      }
    } catch (error) {
      setProviderCartSyncRequired(true)
      setProviderAddNotice({
        offerId,
        isError: true,
        status: error?.status,
        message: mutationError
          ? `${providerCartErrorMessage(mutationError)} The result could not be verified because Provider Offers could not be reloaded. Synchronize before trying again.`
          : 'The add request completed, but Provider Offers could not be reloaded. Synchronize before trying again.',
      })
    } finally {
      providerAddInFlightRef.current = false
      setProviderAddBusyOfferId(null)
    }
  }

  async function retryProviderCartSync(offerId) {
    if (
      providerAddInFlightRef.current ||
      !providerCartSyncRequired ||
      typeof offerId !== 'string'
    ) {
      return
    }

    providerAddInFlightRef.current = true
    setProviderAddBusyOfferId(offerId)

    try {
      const snapshot = await fetchExternalCartSnapshot()
      const item = snapshot.items.find((entry) => entry.offer_id === offerId)
      setProviderCartSyncRequired(false)
      setProviderAddNotice({
        offerId,
        isError: false,
        message: item
          ? `Provider Offers was reloaded. The current quantity is ${item.quantity}; review it before adding again.`
          : 'Provider Offers was reloaded. This offer is not currently listed; you can retry if needed.',
      })
    } catch (error) {
      setProviderAddNotice({
        offerId,
        isError: true,
        status: error?.status,
        message: `${providerCartErrorMessage(error)} Provider Offers could not be synchronized; adding remains disabled.`,
      })
    } finally {
      providerAddInFlightRef.current = false
      setProviderAddBusyOfferId(null)
    }
  }

  function signIn() {
    window.location.assign('/auth/google/login')
  }

  async function signOut() {
    setSignOutPending(true)
    setSignOutError('')

    try {
      const response = await fetch('/auth/logout', {
        method: 'POST',
        credentials: 'same-origin',
      })
      const data = await readApiResponse(response, 'Sign out')
      if (data.authenticated !== false) {
        throw new Error('The sign-out service returned an unexpected response.')
      }

      setAuthStatus('unauthenticated')
      setCart([])
      setCartSubtotal(0)
      setCartCurrency(null)
      setCartLoading(false)
      setCartError('')
      setOrderSuccess('')
      setOrders([])
      setOrdersLoading(false)
      setOrdersError('')
      window.location.replace('/')
      return true
    } catch (error) {
      setSignOutError(
        error instanceof TypeError
          ? 'Could not confirm sign out. Refresh and check your session.'
          : error instanceof Error
            ? error.message
            : 'Could not sign out. Please try again.',
      )
      return false
    } finally {
      setSignOutPending(false)
    }
  }

  function handleCartTabKeyDown(event) {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return

    event.preventDefault()
    const nextSection = cartSection === 'local' ? 'provider' : 'local'
    setCartSection(nextSection)
    event.currentTarget.parentElement
      ?.querySelector(`[data-cart-tab="${nextSection}"]`)
      ?.focus()
  }

  async function sendMessage(event, options = {}) {
    event?.preventDefault?.()
    const text = (options.text ?? chatInput).trim()
    if (!text || isChatLoading) return

    const conversation = [
      ...messages.slice(-10),
      { from: 'user', text },
    ].map((message) => ({
      role: message.from,
      content: message.text,
    }))

    setMessages((current) => [...current, { from: 'user', text }])
    setChatInput('')
    setChatError('')
    setIsChatLoading(true)

    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          messages: conversation,
          selected_products: comparisonSelection.map(({ reference }) => ({
            provider_key: reference.provider_key,
            provider_product_id: reference.provider_product_id,
          })),
          ...(options.cartConfirmationToken || activeCartConfirmationToken
            ? {
                cart_confirmation_token:
                  options.cartConfirmationToken || activeCartConfirmationToken,
              }
            : {}),
        }),
      })

      const data = await readApiResponse(response, 'AI request')

      if (typeof data.reply !== 'string' || !data.reply.trim()) {
        throw new Error('The AI assistant returned an empty response.')
      }
      if (
        data.cart_preview !== undefined &&
        (!isCartPreview(data.cart_preview) ||
          (data.cart_preview.eligible_count > 0 &&
            typeof data.cart_confirmation_token !== 'string'))
      ) {
        throw new Error('The AI assistant returned an invalid cart preview.')
      }

      const previewToken =
        typeof data.cart_confirmation_token === 'string'
          ? data.cart_confirmation_token
          : null
      if (data.cart_preview) {
        setActiveCartConfirmationToken(previewToken)
      } else if (
        data.cart_action?.status === 'added' ||
        data.cart_action?.status === 'already_committed'
      ) {
        setActiveCartConfirmationToken(null)
      }
      setMessages((current) => [
        ...current,
        {
          from: 'assistant',
          text: data.reply,
          cartPreview: data.cart_preview,
          cartConfirmationToken: previewToken,
        },
      ])
      if (authStatus === 'authenticated') {
        await refreshCart(true)
      }
    } catch (error) {
      if (error?.status === 401 || error?.status === 409) {
        setActiveCartConfirmationToken(null)
      }
      const isConfirmationRetry = Boolean(options.cartConfirmationToken)
      const isAmbiguousConfirmationFailure =
        isConfirmationRetry &&
        (error instanceof TypeError ||
          error?.status >= 500 ||
          error?.status === undefined)
      setChatError(
        isAmbiguousConfirmationFailure
          ? 'The confirmation result could not be verified. Select Confirm again to check the saved cart; a committed confirmation will not add products twice.'
          : error instanceof TypeError
            ? 'Could not reach the AI service. Check that the FastAPI backend is running and try again.'
            : error instanceof Error
              ? error.message
              : 'The AI request failed. Please try again.',
      )
    } finally {
      setIsChatLoading(false)
    }
  }

  function toggleVoiceInput() {
    setVoiceInputError('')
    const activeRecognition = voiceRecognitionRef.current
    if (activeRecognition) {
      activeRecognition.stop()
      return
    }

    const SpeechRecognition =
      window.SpeechRecognition || window.webkitSpeechRecognition
    if (!SpeechRecognition) {
      setVoiceInputError('Voice input is not supported in this browser.')
      return
    }

    const recognition = new SpeechRecognition()
    recognition.lang = navigator.language || 'en-US'
    recognition.interimResults = false
    recognition.continuous = false
    voiceInputBaseRef.current = chatInput

    recognition.onstart = () => setIsVoiceListening(true)
    recognition.onresult = (event) => {
      const transcriptParts = []
      for (let index = 0; index < event.results.length; index += 1) {
        const result = event.results[index]
        if (result.isFinal && result[0]?.transcript) {
          transcriptParts.push(result[0].transcript.trim())
        }
      }

      const transcript = transcriptParts.join(' ').trim()
      if (!transcript) return

      const baseText = voiceInputBaseRef.current
      const separator = baseText && !/\s$/.test(baseText) ? ' ' : ''
      setChatInput(`${baseText}${separator}${transcript}`)
    }
    recognition.onerror = (event) => {
      const errorMessages = {
        'not-allowed':
          'Microphone access was denied. Allow microphone access and try again.',
        'service-not-allowed':
          'Microphone access was denied. Allow microphone access and try again.',
        'audio-capture':
          'No microphone was found. Check your microphone and try again.',
        'no-speech': 'No speech was detected. Try again.',
        network: 'Voice recognition could not connect. Check your connection and try again.',
      }
      if (event.error !== 'aborted') {
        setVoiceInputError(
          errorMessages[event.error] || 'Voice input failed. Please try again.',
        )
      }
    }
    recognition.onend = () => {
      if (voiceRecognitionRef.current === recognition) {
        voiceRecognitionRef.current = null
        setIsVoiceListening(false)
      }
    }

    voiceRecognitionRef.current = recognition
    try {
      recognition.start()
    } catch (error) {
      voiceRecognitionRef.current = null
      setIsVoiceListening(false)
      setVoiceInputError(
        error instanceof Error && error.name === 'NotAllowedError'
          ? 'Microphone access was denied. Allow microphone access and try again.'
          : 'Voice input could not start. Check microphone permissions and try again.',
      )
    }
  }

  function confirmCartPreview(token) {
    void sendMessage(null, {
      text: 'Confirm adding eligible products to my cart',
      cartConfirmationToken: token,
    })
  }


  if (activeNav !== 'Dashboard') {
    return (
      <div className="app-shell">
        <aside className="sidebar">
          <div className="brand">
            <div className="brand-mark">D</div>
            <div>
              <span className="brand-name">DealSync</span>
              <span className="brand-caption">SMART SHOPPING</span>
            </div>
          </div>

          <div className="nav-heading">WORKSPACE</div>
          <nav className="nav-list" aria-label="Main navigation">
            {navItems.map((item) => (
              <button
                key={item.label}
                className={`nav-item ${activeNav === item.label ? 'active' : ''}`}
                onClick={() => navigateTo(item.label)}
                aria-label={
                  item.label === 'Compare'
                    ? `Compare products, ${comparisonSelection.length} selected`
                    : undefined
                }
              >
                <span className="nav-icon">{item.icon}</span>
                <span>{item.label}</span>
                {item.label === 'My Cart' && cartCount > 0 && (
                  <span className="nav-count">{cartCount}</span>
                )}
                {item.label === 'Compare' && comparisonSelection.length > 0 && (
                  <span className="nav-count">{comparisonSelection.length}</span>
                )}
              </button>
            ))}
          </nav>

        </aside>

        <main className="main-area">
          <header className="topbar">
            <div className="breadcrumb">Workspace <span>/</span> {activeNav}</div>
            <div className="topbar-actions">
              <span className="preview-label"><span className="status-dot" /> Frontend preview</span>
              <AccountControl
                key={authStatus === 'authenticated' ? 'signed-in' : 'signed-out'}
                isAuthenticated={authStatus === 'authenticated'}
                signOutPending={signOutPending}
                signOutError={signOutError}
                onSignIn={signIn}
                onSignOut={signOut}
              />
            </div>
          </header>

          <div className="page-content workspace-page">
            {activeNav === 'Compare' && (
              <section className="workspace-panel comparison-page">
                <div className="workspace-page-heading">
                  <div className="eyebrow">
                    {hasDemoComparisonSelection
                      ? 'PRODUCT COMPARISON'
                      : 'VERIFIED PRODUCT EVIDENCE'}
                  </div>
                  <h1>Compare products</h1>
                  <p>
                    {hasDemoComparisonSelection
                      ? 'Compare selected provider data. Demo entries are sample data, not live marketplace offers.'
                      : 'Compare provider offers using server-verified information. Unknown facts stay unknown, and prices remain grouped by currency.'}
                  </p>
                </div>

                {comparisonSelection.length === 0 ? (
                  <div className="workspace-empty comparison-empty">
                    <div className="assistant-welcome-icon">⇄</div>
                    <h2>Select products to compare</h2>
                    <p>
                      Add two to five offers from text or image discovery results.
                      Catalog items without provider references cannot be compared here.
                    </p>
                    <div className="comparison-empty-actions">
                      <button
                        className="hero-button"
                        type="button"
                        onClick={() => {
                          pendingOfferSearchScrollRef.current = true
                          setActiveNav('Products')
                        }}
                      >
                        Find offers
                      </button>
                      <button
                        className="text-button"
                        type="button"
                        onClick={() => setActiveNav('Shop by Image')}
                      >
                        Shop by image
                      </button>
                    </div>
                  </div>
                ) : (
                  <section className="comparison-selection" aria-labelledby="comparison-selection-title">
                    <div className="section-heading">
                      <div>
                        <div className="eyebrow">YOUR SHORTLIST</div>
                        <h2 id="comparison-selection-title">
                          Selected products ({comparisonSelection.length}/5)
                        </h2>
                      </div>
                      <button
                        className="text-button"
                        type="button"
                        onClick={() => {
                          setComparisonSelection([])
                          setActiveCartConfirmationToken(null)
                          resetComparisonResult()
                        }}
                      >
                        Clear selection
                      </button>
                    </div>
                    <ul className="comparison-selected-list">
                      {comparisonSelection.map((item) => {
                        const key = comparisonReferenceKey(item.reference)
                        return (
                          <li key={key}>
                            <div>
                              <strong>{item.offer.product_name || 'Name unavailable'}</strong>
                              <span>{item.providerName}</span>
                              {item.reference.provider_key === 'demo_catalog' && (
                                <DemoOfferNotice />
                              )}
                              <small>
                                {item.reference.provider_key} · product reference{' '}
                                {item.reference.provider_product_id}
                              </small>
                              <small>
                                {formatOfferPrice(item.offer.price_minor, item.offer.currency)}
                              </small>
                            </div>
                            <button
                              className="text-button"
                              type="button"
                              aria-label={`Remove ${item.offer.product_name || 'product'} from comparison`}
                              onClick={() => toggleComparisonSelection(item.offer, item.providerName)}
                            >
                              Remove
                            </button>
                          </li>
                        )
                      })}
                    </ul>
                    {comparisonSelection.some((item) => item.discoveryPartial) && (
                      <p className="image-search-partial" role="status">
                        {hasDemoComparisonSelection
                          ? 'At least one selected offer came from a partial provider search. This comparison evaluates only selected products; demo entries remain sample data, and omitted results are not included.'
                          : 'At least one selected offer came from a partial provider search. This comparison evaluates only the selected products and verified server-side evidence; omitted results are not included.'}
                      </p>
                    )}
                    {comparisonSelection.length < 2 && (
                      <p className="comparison-hint" role="status">
                        Select at least one more distinct provider product.
                      </p>
                    )}
                    {comparisonSelection.some(
                      (item) =>
                        item.reference.provider_key === 'demo_catalog',
                    ) && (
                      <DemoOfferNotice />
                    )}
                  </section>
                )}

                <div className="comparison-controls">
                  <form className="comparison-requirements" onSubmit={submitComparison}>
                    <h2>Optional requirements</h2>
                    <p>
                      Only enter a budget when you know its currency. Requirements are
                      evaluated deterministically against available evidence.
                    </p>

                    <div className="comparison-filter-grid">
                      <label className="offer-filter">
                        Maximum budget
                        <input
                          type="number"
                          min="0"
                          step="any"
                          value={comparisonBudgetAmount}
                          onChange={(event) => {
                            setComparisonBudgetAmount(event.target.value)
                            resetComparisonResult()
                          }}
                          aria-label="Comparison maximum budget amount"
                          placeholder="Optional"
                        />
                      </label>
                      <label className="offer-filter">
                        Budget currency
                        <select
                          value={comparisonBudgetCurrency}
                          onChange={(event) => {
                            setComparisonBudgetCurrency(event.target.value)
                            resetComparisonResult()
                          }}
                          aria-label="Comparison budget currency"
                          required={Boolean(comparisonBudgetAmount.trim())}
                        >
                          <option value="">Choose currency</option>
                          {IMAGE_SEARCH_CURRENCIES.map((currency) => (
                            <option key={currency} value={currency}>{currency}</option>
                          ))}
                        </select>
                      </label>
                    </div>

                    <div className="comparison-criterion-builder">
                      <label className="offer-filter">
                        Must-have attribute
                        <select
                          value={comparisonAttribute}
                          onChange={(event) => {
                            const attribute = event.target.value
                            setComparisonAttribute(attribute)
                            setComparisonExpectedValue(
                              attribute === 'is_organic' ? 'true' : '',
                            )
                          }}
                          aria-label="Must-have attribute"
                        >
                          <option value="is_organic">Organic</option>
                          <option value="minimum_rating">Minimum rating</option>
                          <option value="brand">Brand</option>
                          <option value="model">Model</option>
                          <option value="category">Category</option>
                          <option value="product_name">Product name</option>
                          <option value="availability">Availability</option>
                          <option value="description_contains">Description contains</option>
                        </select>
                      </label>
                      {comparisonAttribute === 'is_organic' ? (
                        <label className="offer-filter">
                          Required value
                          <select
                            value={comparisonExpectedValue}
                            onChange={(event) => setComparisonExpectedValue(event.target.value)}
                            aria-label="Organic requirement value"
                          >
                            <option value="true">Yes</option>
                            <option value="false">No</option>
                          </select>
                        </label>
                      ) : (
                        <label className="offer-filter">
                          Required value
                          <input
                            type={comparisonAttribute === 'minimum_rating' ? 'number' : 'text'}
                            min={comparisonAttribute === 'minimum_rating' ? '0' : undefined}
                            max={comparisonAttribute === 'minimum_rating' ? '5' : undefined}
                            step={comparisonAttribute === 'minimum_rating' ? '0.1' : undefined}
                            maxLength={120}
                            value={comparisonExpectedValue}
                            onChange={(event) => setComparisonExpectedValue(event.target.value)}
                            aria-label="Must-have expected value"
                            placeholder={
                              comparisonAttribute === 'minimum_rating'
                                ? '0–5'
                                : 'Enter a value'
                            }
                          />
                        </label>
                      )}
                      <button
                        className="offer-search-button comparison-add-criterion"
                        type="button"
                        onClick={addMustHaveCriterion}
                        disabled={comparisonMustHaves.length >= 8}
                      >
                        Add requirement
                      </button>
                    </div>

                    {comparisonMustHaves.length > 0 && (
                      <ul className="comparison-requirement-list">
                        {comparisonMustHaves.map((criterion, index) => (
                          <li key={`${criterion.attribute}-${index}`}>
                            <span>
                              {criterion.attribute.replaceAll('_', ' ')}: {displayEvidenceValue(criterion.expected_value)}
                            </span>
                            <button
                              type="button"
                              className="text-button"
                              aria-label={`Remove ${criterion.attribute.replaceAll('_', ' ')} requirement`}
                              onClick={() => {
                                setComparisonMustHaves((current) =>
                                  current.filter((_, itemIndex) => itemIndex !== index),
                                )
                                resetComparisonResult()
                              }}
                            >
                              Remove
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}

                    {authStatus === 'loading' && (
                      <p className="comparison-auth-message" role="status">
                        Checking your sign-in...
                      </p>
                    )}
                    {(comparisonAuthRequired || authStatus === 'unauthenticated') && (
                      <div className="comparison-auth-message">
                        <p>Sign in with Google to compare products.</p>
                        <button className="hero-button" type="button" onClick={signIn}>
                          Sign in with Google
                        </button>
                      </div>
                    )}
                    {authStatus === 'error' && (
                      <div className="comparison-auth-message" role="alert">
                        <p>{cartError}</p>
                        <button className="text-button" type="button" onClick={refreshAuthentication}>
                          Retry sign-in check
                        </button>
                      </div>
                    )}
                    {comparisonInvalidSelection && comparisonError && (
                      <p className="comparison-error" role="alert">{comparisonError}</p>
                    )}
                    {!comparisonInvalidSelection && comparisonError && (
                      <p className="comparison-error" role="alert">{comparisonError}</p>
                    )}
                    <button
                      className="offer-search-button comparison-submit"
                      type="submit"
                      disabled={comparisonLoading || comparisonSelection.length < 2}
                    >
                      {comparisonLoading
                        ? hasDemoComparisonSelection
                          ? 'Comparing selected products...'
                          : 'Comparing verified products...'
                        : 'Compare selected products'}
                    </button>
                  </form>
                </div>

                {comparisonLoading && (
                  <div className="empty-results" role="status">
                    {hasDemoComparisonSelection
                      ? 'Loading comparison data and evaluating your requirements...'
                      : 'Loading verified product evidence and evaluating your requirements...'}
                  </div>
                )}
                {comparisonResult && (
                  <ComparisonResultView
                    result={comparisonResult}
                    providers={providers}
                    providersLoading={providersLoading}
                    providerAddBusyOfferId={providerAddBusyOfferId}
                    providerAddNotice={providerAddNotice}
                    providerCartSyncRequired={providerCartSyncRequired}
                    onAddToProviderCart={addToProviderCart}
                    onRetryProviderCartSync={retryProviderCartSync}
                    onSignIn={signIn}
                  />
                )}
              </section>
            )}

            {activeNav === 'Products' && (
              <>
                <div className="workspace-page-heading">
                  <div className="eyebrow">YOUR LIVE PRODUCT CATALOG</div>
                  <h1>Explore products</h1>
                  <p>Search and browse products from your database.</p>
                </div>

                <div className="search-box">
                  <span className="search-icon">?</span>
                  <input
                    ref={productSearchInputRef}
                    value={query}
                    onChange={(event) => setQuery(event.target.value)}
                    placeholder="Search products..."
                    aria-label="Search products"
                  />
                  <span className="search-shortcut">SEARCH</span>
                </div>

                <div className="category-row">
                  {categoryOptions.map((item) => (
                    <button
                      key={item}
                      className={`category-chip ${category === item ? 'selected' : ''}`}
                      onClick={() => setCategory(item)}
                    >
                      {item === 'All'
                        ? 'All'
                        : item.charAt(0).toUpperCase() + item.slice(1)}
                    </button>
                  ))}
                </div>

                {productsLoading && (
                  <div className="empty-results" role="status">
                    Loading products from your database...
                  </div>
                )}

                {productsError && (
                  <div className="empty-results" role="alert">
                    {productsError}
                  </div>
                )}

                {!productsLoading && !productsError && (
                  <>
                    <div className="product-grid workspace-product-grid">
                      {filteredProducts.map((product) => (
                        <ProductCard
                          key={product.id}
                          product={product}
                          onAdd={addToCart}
                          canAdd={authStatus === 'authenticated'}
                          isAdding={cartOperation === `add-${product.id}`}
                        />
                      ))}

                      {filteredProducts.length === 0 && (
                        <div className="empty-results">
                          {apiProducts.length === 0
                            ? 'No products are available in the catalog.'
                            : 'No matching products found. Try another search or category.'}
                        </div>
                      )}
                    </div>

                    <p className="demo-note">
                      Showing {filteredProducts.length} of {apiProducts.length} database products. Legacy catalog currency is unverified; amounts are not converted.
                    </p>
                  </>
                )}

                <section className="offer-search-panel" ref={offerSearchPanelRef}>
                  <div className="section-heading">
                    <div>
                      <div className="eyebrow">REGISTERED PROVIDERS</div>
                      <h2>Search offers</h2>
                    </div>
                  </div>

                  {providersLoading && (
                    <div className="empty-results" role="status">
                      Loading registered providers...
                    </div>
                  )}
                  {providersError && (
                    <div className="empty-results" role="alert">
                      {providersError}
                    </div>
                  )}

                  {!providersLoading && providers.length > 0 && (
                    <section
                      className="registered-provider-list"
                      aria-label="Registered provider information"
                    >
                      {providers.map((provider) => {
                        const description =
                          provider.provider_key === 'local_catalog'
                            ? 'Products from the DealSync local catalog. Currency and availability may be unknown.'
                            : provider.provider_key === 'demo_catalog'
                              ? 'Synthetic sample products for discovery; not live marketplace listings or prices.'
                              : 'Products returned by this registered provider.'

                        return (
                          <article
                            className="registered-provider-card"
                            key={provider.provider_key}
                          >
                            <ProviderBadge
                              providerKey={provider.provider_key}
                              providerName={provider.display_name}
                            />
                            <p>{description}</p>
                            <span className="provider-capability">
                              {provider.supports_external_cart
                                ? 'External-cart eligible'
                                : 'Discovery only'}
                            </span>
                            {provider.provider_key === 'demo_catalog' && (
                              <DemoOfferNotice />
                            )}
                          </article>
                        )
                      })}
                    </section>
                  )}

                  <form className="offer-search-form" onSubmit={searchOffers}>
                    <label className="offer-filter">
                      Search mode
                      <select
                        value={offerSearchMode}
                        disabled={offersLoading}
                        onChange={(event) => {
                          setOfferSearchMode(event.target.value)
                          setOffers([])
                          setOffersSearched(false)
                          setOffersPartial(false)
                          setOffersError('')
                          setSmartSearchResult(null)
                        }}
                        aria-label="Search mode"
                      >
                        <option value="offers">Provider offers</option>
                        <option value="smart">Smart Search</option>
                      </select>
                    </label>
                    <label className="offer-filter">
                      Search
                      <input
                        type="search"
                        value={offerQuery}
                        onChange={(event) => setOfferQuery(event.target.value)}
                        placeholder={
                          offerSearchMode === 'smart'
                            ? 'e.g. Laptop under ₹50,000'
                            : 'Search provider offers'
                        }
                        aria-label={
                          offerSearchMode === 'smart'
                            ? 'Describe what you want to shop for'
                            : 'Search provider offers'
                        }
                        required={offerSearchMode === 'smart'}
                      />
                    </label>
                    {offerSearchMode === 'offers' && (
                      <>
                        <label className="offer-filter">
                          Product category
                          <select
                            value={
                              categoryOptions.slice(1).includes(offerQuery)
                                ? offerQuery
                                : ''
                            }
                            onChange={(event) =>
                              setOfferQuery(event.target.value)
                            }
                            aria-label="Search provider offers by product category"
                          >
                            <option value="">All categories</option>
                            {categoryOptions
                              .filter((item) => item !== 'All')
                              .map((item) => (
                                <option key={item} value={item}>
                                  {item}
                                </option>
                              ))}
                          </select>
                        </label>
                        <label className="offer-filter">
                          Provider
                          <select
                            value={offerProviderKey}
                            onChange={(event) =>
                              setOfferProviderKey(event.target.value)
                            }
                            aria-label="Provider"
                          >
                            <option value="">All registered providers</option>
                            {providers.map((provider) => (
                              <option
                                key={provider.provider_key}
                                value={provider.provider_key}
                              >
                                {provider.display_name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label className="offer-filter">
                          Maximum price (INR)
                          <input
                            type="number"
                            min="0"
                            step="0.01"
                            value={offerMaxPrice}
                            onChange={(event) =>
                              setOfferMaxPrice(event.target.value)
                            }
                            aria-label="Maximum price in INR"
                          />
                        </label>
                        <label className="offer-filter">
                          Organic
                          <select
                            value={offerOrganic}
                            onChange={(event) =>
                              setOfferOrganic(event.target.value)
                            }
                            aria-label="Organic filter"
                          >
                            <option value="">Any</option>
                            <option value="true">Organic</option>
                            <option value="false">Non-organic</option>
                          </select>
                        </label>
                      </>
                    )}
                    <button
                      className="offer-search-button"
                      type="submit"
                      disabled={offersLoading}
                    >
                      {offersLoading
                        ? 'Searching...'
                        : offerSearchMode === 'smart'
                          ? 'Smart Search'
                          : 'Search offers'}
                    </button>
                  </form>

                  {offersLoading && (
                    <div className="empty-results" role="status">
                      {offerSearchMode === 'smart'
                        ? 'Understanding your request and searching providers...'
                        : 'Searching registered providers...'}
                    </div>
                  )}
                  {offersError && (
                    <div className="empty-results" role="alert">
                      {offersError}
                    </div>
                  )}
                  {!offersLoading &&
                    !offersError &&
                    offersSearched &&
                    offerSearchMode === 'smart' &&
                    smartSearchResult && (
                      <section
                        className="smart-search-summary"
                        aria-label="Smart Search details"
                      >
                        <div className="smart-search-summary-grid">
                          <div className="smart-search-detail">
                            <span>Original query</span>
                            <strong>{smartSearchResult.original_query}</strong>
                          </div>
                          <div className="smart-search-detail">
                            <span>Product category</span>
                            <strong>
                              {smartSearchResult.product_category ||
                                'Not identified'}
                            </strong>
                          </div>
                          <div className="smart-search-detail">
                            <span>Budget</span>
                            <strong>
                              {smartSearchResult.budget_amount == null
                                ? 'No budget specified'
                                : `${smartSearchResult.budget_amount} ${
                                    smartSearchResult.budget_currency ||
                                    'currency needed'
                                  }`}
                            </strong>
                          </div>
                        </div>
                        <div className="smart-search-requirements">
                          <div>
                            <strong>Detected preferences</strong>
                            {smartSearchResult.preferences.length > 0 ? (
                              <ul>
                                {smartSearchResult.preferences.map(
                                  (preference, index) => (
                                    <li key={`${preference}-${index}`}>
                                      {preference}
                                    </li>
                                  ),
                                )}
                              </ul>
                            ) : (
                              <p>None identified</p>
                            )}
                          </div>
                          <div>
                            <strong>Unsupported requirements</strong>
                            {smartSearchResult.unsupported_requirements.length >
                            0 ? (
                              <ul>
                                {smartSearchResult.unsupported_requirements.map(
                                  (requirement, index) => (
                                    <li key={`${requirement}-${index}`}>
                                      {requirement}
                                    </li>
                                  ),
                                )}
                              </ul>
                            ) : (
                              <p>None identified</p>
                            )}
                          </div>
                        </div>
                        {smartSearchResult.clarification_required && (
                          <p
                            className="smart-search-clarification"
                            role="alert"
                          >
                            {smartSearchResult.clarification_message ||
                              'Please clarify your budget before searching.'}
                          </p>
                        )}
                        {smartSearchResult.providers.length > 0 && (
                          <div className="smart-search-providers">
                            <strong>Provider outcomes</strong>
                            <ul>
                              {smartSearchResult.providers.map((provider) => {
                                const providerName =
                                  providers.find(
                                    (item) =>
                                      item.provider_key ===
                                      provider.provider_key,
                                  )?.display_name || provider.provider_key

                                return (
                                  <li key={provider.provider_key}>
                                    <span>
                                      {providerName}: {provider.status}
                                      {Number.isInteger(provider.offer_count) &&
                                        ` (${provider.offer_count} offers)`}
                                    </span>
                                    {provider.error_message && (
                                      <small>{provider.error_message}</small>
                                    )}
                                  </li>
                                )
                              })}
                            </ul>
                          </div>
                        )}
                      </section>
                    )}
                  {!offersLoading && !offersError && offersSearched && offersPartial && (
                    <p className="image-search-partial" role="status">
                      {offerSearchMode === 'smart'
                        ? 'Some providers did not complete successfully. Available results are shown.'
                        : 'Some providers did not complete successfully. Available offers are shown; comparisons cover only the products you select.'}
                    </p>
                  )}
                  {!offersLoading &&
                    !offersError &&
                    offersSearched &&
                    offers.length > 0 && (
                    <div className="product-grid offer-results-grid">
                      {offers.map((offer) => {
                        const providerName =
                          providers.find(
                            (provider) =>
                              provider.provider_key === offer.provider_key,
                          )?.display_name || offer.provider_key

                        return (
                          <OfferCard
                            key={offer.offer_id || `${offer.provider_key}-${offer.provider_offer_key}`}
                            offer={offer}
                            providerName={providerName}
                            onToggleComparison={toggleComparisonSelection}
                            discoveryPartial={offersPartial}
                            providers={providers}
                            providersLoading={providersLoading}
                            providerAddBusyOfferId={providerAddBusyOfferId}
                            providerAddNotice={
                              providerAddNotice?.offerId === offer.offer_id
                                ? providerAddNotice
                                : null
                            }
                            providerCartSyncRequired={providerCartSyncRequired}
                            onAddToProviderCart={addToProviderCart}
                            onRetryProviderCartSync={retryProviderCartSync}
                            onSignIn={signIn}
                            isSelected={comparisonSelection.some(
                              (item) =>
                                comparisonReferenceKey(item.reference) ===
                                comparisonReferenceKey({
                                  provider_key: offer.provider_key,
                                  provider_product_id: offer.provider_product_id,
                                }),
                            )}
                            selectionDisabled={
                              comparisonSelection.length >= 5 &&
                              !comparisonSelection.some(
                                (item) =>
                                  comparisonReferenceKey(item.reference) ===
                                  comparisonReferenceKey({
                                    provider_key: offer.provider_key,
                                    provider_product_id: offer.provider_product_id,
                                  }),
                              )
                            }
                          />
                        )
                      })}
                    </div>
                  )}
                  {!offersLoading &&
                    !offersError &&
                    offersSearched &&
                    offers.length === 0 &&
                    !smartSearchResult?.clarification_required && (
                      <div className="empty-results" role="status">
                        {offerSearchMode === 'smart'
                          ? 'No matching products found. Try another search or include more product details.'
                          : 'No offers match your search.'}
                      </div>
                    )}
                  <p className="demo-note">
                    Provider offers are shown separately and are not connected
                    to the existing cart.
                  </p>
                </section>
              </>
            )}

            {activeNav === 'AI Assistant' && (
              <section className="workspace-panel">
                <div className="workspace-page-heading">
                  <div className="eyebrow">YOUR SHOPPING COMPANION</div>
                  <h1>AI Assistant</h1>
                  <p>Ask shopping questions and get help from your DealSync assistant.</p>
                </div>

                <div className="chat-messages workspace-chat" aria-live="polite">
                  {messages.map((message, index) => (
                    <div className={`chat-message ${message.from}`} key={index}>
                      {message.from === 'assistant' && <span className="message-avatar">?</span>}
                      {message.from === 'assistant' ? (
                        <div className="message-content">
                          <ReactMarkdown remarkPlugins={[remarkGfm]}>
                            {message.text}
                          </ReactMarkdown>
                          {message.cartPreview && (
                            <CartPreviewCard
                              preview={message.cartPreview}
                              canConfirm={
                                Boolean(message.cartConfirmationToken) &&
                                message.cartConfirmationToken === activeCartConfirmationToken
                              }
                              isLoading={isChatLoading}
                              onConfirm={() =>
                                confirmCartPreview(message.cartConfirmationToken)
                              }
                            />
                          )}
                        </div>
                      ) : (
                        <p>{message.text}</p>
                      )}
                    </div>
                  ))}
                  {isChatLoading && (
                    <div className="chat-message assistant" role="status">
                      <span className="message-avatar">?</span>
                      <p className="message-content">Thinking...</p>
                    </div>
                  )}
                </div>
                {chatError && <p className="chat-error" role="alert">{chatError}</p>}

                <div className="suggestions">
                  <button onClick={() => setChatInput('Find affordable headphones')}>Affordable headphones</button>
                  <button onClick={() => setChatInput('Help me compare products')}>Compare products</button>
                </div>

                <form className="chat-form" onSubmit={sendMessage}>
                  <input
                    value={chatInput}
                    onChange={(event) => setChatInput(event.target.value)}
                    placeholder="Ask me anything..."
                    aria-label="Message the shopping assistant"
                    disabled={isChatLoading}
                  />
                  <button
                    className={`chat-voice-button${isVoiceListening ? ' is-listening' : ''}`}
                    type="button"
                    aria-label={isVoiceListening ? 'Stop voice input' : 'Start voice input'}
                    aria-pressed={isVoiceListening}
                    title="Voice input"
                    onClick={toggleVoiceInput}
                  >
                    <svg
                      aria-hidden="true"
                      viewBox="0 0 24 24"
                      width="16"
                      height="16"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <rect x="9" y="2" width="6" height="12" rx="3" />
                      <path d="M5 10v1a7 7 0 0 0 14 0v-1M12 18v4m-4 0h8" />
                    </svg>
                  </button>
                  <button type="submit" aria-label="Send message" disabled={isChatLoading}>
                    <svg
                      aria-hidden="true"
                      viewBox="0 0 24 24"
                      width="16"
                      height="16"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    >
                      <circle cx="11" cy="11" r="7" />
                      <path d="m20 20-4-4" />
                    </svg>
                  </button>
                </form>
                {voiceInputError ? (
                  <p className="voice-input-feedback error" role="alert">
                    {voiceInputError}
                  </p>
                ) : isVoiceListening ? (
                  <p className="voice-input-feedback" role="status">
                    Listening... Speak now.
                  </p>
                ) : null}
                <p className="demo-note">AI responses are generated by the DealSync shopping assistant.</p>
              </section>
            )}

            {activeNav === 'Shop by Image' && (
              <section className="workspace-panel">
                <div className="workspace-page-heading">
                  <div className="eyebrow">VISUAL PRODUCT DISCOVERY</div>
                  <h1>Shop by Image</h1>
                  <p>Upload a product image to discover related offers.</p>
                </div>
                <div className="workspace-upload">
                  <h2>Choose a product image</h2>
                  <p>Select a JPEG, PNG or WebP image. It will only be uploaded when you start the search.</p>
                  <div className="image-selection-options">
                    <label className="image-file-picker">
                      <span>{selectedImage ? 'Upload a different image' : 'Upload Image'}</span>
                      <input
                        ref={imageFileInputRef}
                        type="file"
                        accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/jpg,image/png,image/webp"
                        aria-label="Upload a product image"
                        onChange={handleImageSelection}
                        disabled={imageSearchLoading}
                      />
                    </label>
                    <button
                      className="image-camera-button"
                      type="button"
                      onClick={() => void startCamera()}
                      disabled={imageSearchLoading || isCameraOpen}
                    >
                      <svg
                        className="image-camera-icon"
                        aria-hidden="true"
                        viewBox="0 0 24 24"
                        width="18"
                        height="18"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >
                        <path d="M14 4h-4l-2 3H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3z" />
                        <circle cx="12" cy="13" r="3" />
                      </svg>
                      Take Photo
                    </button>
                  </div>
                  {cameraError && (
                    <p className="image-search-error" role="alert">
                      {cameraError}
                    </p>
                  )}
                  {isCameraOpen && (
                    <div className="image-camera-panel">
                      {cameraDevices.length > 1 && (
                        <label className="offer-filter image-camera-selector">
                          Camera
                          <select
                            value={activeCameraId}
                            onChange={(event) => void startCamera(event.target.value)}
                            aria-label="Select camera"
                          >
                            {cameraDevices.map((device, index) => (
                              <option key={device.deviceId} value={device.deviceId}>
                                {device.label || `Camera ${index + 1}`}
                              </option>
                            ))}
                          </select>
                        </label>
                      )}
                      <video
                        ref={cameraVideoRef}
                        className="image-camera-preview"
                        autoPlay
                        muted
                        playsInline
                        aria-label="Live camera preview"
                      />
                      <div className="image-camera-controls">
                        <button
                          className="hero-button"
                          type="button"
                          onClick={capturePhoto}
                        >
                          Capture Photo
                        </button>
                        <button
                          className="text-button"
                          type="button"
                          onClick={stopCamera}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                  {selectedImage && (
                    <div className="image-preview">
                      {imagePreviewUrl && (
                        <img
                          src={imagePreviewUrl}
                          alt={`Preview of ${selectedImage.name}`}
                        />
                      )}
                      <div className="image-preview-details">
                        <strong>{selectedImage.name}</strong>
                        <span>
                          {(selectedImage.size / (1024 * 1024)).toFixed(2)} MiB
                        </span>
                      </div>
                      <div className="image-preview-actions">
                        {isCapturedImage && (
                          <button
                            className="text-button"
                            type="button"
                            onClick={() => void startCamera(activeCameraId)}
                            disabled={imageSearchLoading}
                          >
                            Retake
                          </button>
                        )}
                        <button
                          className="text-button image-remove-button"
                          type="button"
                          onClick={clearImageSelection}
                          disabled={imageSearchLoading}
                        >
                          Remove image
                        </button>
                      </div>
                    </div>
                  )}

                  <form className="image-search-form" onSubmit={searchByImage}>
                    <label className="offer-filter">
                      Provider
                      <select
                        value={imageProviderKey}
                        onChange={(event) => setImageProviderKey(event.target.value)}
                        aria-label="Image search provider"
                        disabled={providersLoading || imageSearchLoading}
                      >
                        <option value="">All registered providers</option>
                        {providers.map((provider) => (
                          <option
                            key={provider.provider_key}
                            value={provider.provider_key}
                          >
                            {provider.display_name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="offer-filter">
                      Maximum price
                      <input
                        type="number"
                        min="0"
                        step="any"
                        value={imageMaxPrice}
                        onChange={(event) => setImageMaxPrice(event.target.value)}
                        aria-label="Image search maximum price"
                        disabled={imageSearchLoading}
                      />
                    </label>
                    <label className="offer-filter">
                      Currency (required with maximum price)
                      <select
                        value={imageCurrency}
                        onChange={(event) => setImageCurrency(event.target.value)}
                        aria-label="Image search currency"
                        required={Boolean(imageMaxPrice.trim())}
                        disabled={imageSearchLoading}
                      >
                        <option value="">Choose currency</option>
                        {IMAGE_SEARCH_CURRENCIES.map((currency) => (
                          <option key={currency} value={currency}>
                            {currency}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="offer-filter">
                      Organic filter
                      <select
                        value={imageOrganic}
                        onChange={(event) => setImageOrganic(event.target.value)}
                        aria-label="Image search organic filter"
                        disabled={imageSearchLoading}
                      >
                        <option value="">Any</option>
                        <option value="true">Organic</option>
                        <option value="false">Non-organic</option>
                      </select>
                    </label>
                    <button
                      className="offer-search-button image-search-button"
                      type="submit"
                      disabled={
                        imageSearchLoading ||
                        imageSearchRetrySeconds > 0 ||
                        !selectedImage ||
                        authStatus !== 'authenticated'
                      }
                    >
                      {imageSearchLoading ? 'Searching...' : 'Search by Image'}
                    </button>
                  </form>

                  {authStatus === 'loading' && (
                    <p className="image-search-message" role="status">
                      Checking your sign-in...
                    </p>
                  )}
                  {authStatus === 'unauthenticated' && (
                    <div className="image-search-auth">
                      <p>Sign in with Google to search for offers.</p>
                      <button
                        className="hero-button"
                        type="button"
                        onClick={signIn}
                      >
                        Sign in with Google
                      </button>
                    </div>
                  )}
                  {authStatus === 'error' && (
                    <div className="image-search-auth" role="alert">
                      <p>{cartError}</p>
                      <button
                        className="text-button"
                        type="button"
                        onClick={refreshAuthentication}
                      >
                        Retry sign-in check
                      </button>
                    </div>
                  )}
                  {(imageSearchError || imageSearchRetrySeconds > 0) && (
                    <p className="image-search-error" role="alert">
                      {imageSearchRetrySeconds > 0
                        ? `You've reached your image search limit. Try again in ${Math.floor(imageSearchRetrySeconds / 60)} min ${imageSearchRetrySeconds % 60} sec.`
                        : imageSearchError}
                    </p>
                  )}
                  {imageSearchLoading && (
                    <div className="empty-results" role="status">
                      Analyzing the image and searching registered providers...
                    </div>
                  )}
                </div>

                {imageSearchSearched && imageSearchResult && (
                  <section className="image-search-results" aria-live="polite">
                    {imageSearchResult.partial === true && (
                      <p className="image-search-partial" role="status">
                        Some providers did not complete successfully. Available results are shown below.
                      </p>
                    )}

                    <div className="image-result-summary">
                      <h2>Visual analysis</h2>
                      <p>
                        Search query:{' '}
                        {imageSearchResult.search_query || 'No usable search query identified.'}
                      </p>
                      {imageSearchResult.visual_analysis.description && (
                        <p>{imageSearchResult.visual_analysis.description}</p>
                      )}
                      <ul>
                        {[
                          imageSearchResult.visual_analysis.product_category &&
                            `Category: ${imageSearchResult.visual_analysis.product_category}`,
                          imageSearchResult.visual_analysis.product_type &&
                            `Product type: ${imageSearchResult.visual_analysis.product_type}`,
                          typeof imageSearchResult.visual_analysis.is_organic === 'boolean' &&
                            `Organic: ${imageSearchResult.visual_analysis.is_organic ? 'Yes' : 'No'}`,
                          imageSearchResult.visual_analysis.brand &&
                            `Brand: ${imageSearchResult.visual_analysis.brand}`,
                          imageSearchResult.visual_analysis.model &&
                            `Model: ${imageSearchResult.visual_analysis.model}`,
                          imageSearchResult.visual_analysis.primary_color &&
                            `Primary color: ${imageSearchResult.visual_analysis.primary_color}`,
                          imageSearchResult.visual_analysis.secondary_colors?.length &&
                            `Other colors: ${imageSearchResult.visual_analysis.secondary_colors.join(', ')}`,
                          imageSearchResult.visual_analysis.shape_or_design &&
                            `Shape or design: ${imageSearchResult.visual_analysis.shape_or_design}`,
                          imageSearchResult.visual_analysis.distinctive_features?.length &&
                            `Distinctive features: ${imageSearchResult.visual_analysis.distinctive_features.join(', ')}`,
                          imageSearchResult.visual_analysis.visible_text?.length &&
                            `Visible text: ${imageSearchResult.visual_analysis.visible_text.join(', ')}`,
                        ]
                          .filter(Boolean)
                          .map((detail) => <li key={detail}>{detail}</li>)}
                      </ul>
                      <p className="demo-note">
                        Relevance scores are uncalibrated catalog-text heuristics, not probabilities or guarantees of product identity.
                      </p>
                    </div>

                    <div className="image-result-section">
                      <h2>Provider status</h2>
                      {imageSearchResult.providers.length > 0 ? (
                        <ul className="image-provider-list">
                          {imageSearchResult.providers.map((outcome) => {
                            const providerName =
                              providers.find(
                                (provider) =>
                                  provider.provider_key === outcome.provider_key,
                              )?.display_name || outcome.provider_key
                            return (
                              <li key={outcome.provider_key}>
                                <strong>{providerName}</strong>
                                <span>Status: {outcome.status}</span>
                                <span>Offers: {outcome.offer_count}</span>
                              </li>
                            )
                          })}
                        </ul>
                      ) : (
                        <p className="image-search-message">
                          No providers were queried because no usable search query was returned.
                        </p>
                      )}
                    </div>

                    {imageSearchResult.price_summaries.length > 0 && (
                      <div className="image-result-section">
                        <h2>Price summaries by currency</h2>
                        <ul className="image-price-summary-list">
                          {imageSearchResult.price_summaries.map((summary) => (
                            <li key={summary.currency}>
                              <strong>{summary.currency}</strong>
                              <span>
                                Minimum: {formatOfferPrice(
                                  summary.minimum_price_minor,
                                  summary.currency,
                                )}
                              </span>
                              <span>
                                Maximum: {formatOfferPrice(
                                  summary.maximum_price_minor,
                                  summary.currency,
                                )}
                              </span>
                              <span>Offers: {summary.offer_count}</span>
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}

                    <div className="image-result-section">
                      <h2>Ranked offers</h2>
                      {imageSearchResult.offers.length > 0 ? (
                        <div className="product-grid offer-results-grid">
                          {imageSearchResult.offers.map((offer) => {
                            const providerName =
                              providers.find(
                                (provider) =>
                                  provider.provider_key === offer.provider_key,
                              )?.display_name || offer.provider_key
                            return (
                              <OfferCard
                                key={offer.offer_id}
                                offer={offer}
                                providerName={providerName}
                                ranking={imageRankings.get(offer.offer_id)}
                                onToggleComparison={toggleComparisonSelection}
                                discoveryPartial={imageSearchResult.partial === true}
                                providers={providers}
                                providersLoading={providersLoading}
                                providerAddBusyOfferId={providerAddBusyOfferId}
                                providerAddNotice={
                                  providerAddNotice?.offerId === offer.offer_id
                                    ? providerAddNotice
                                    : null
                                }
                                providerCartSyncRequired={providerCartSyncRequired}
                                onAddToProviderCart={addToProviderCart}
                                onRetryProviderCartSync={retryProviderCartSync}
                                onSignIn={signIn}
                                isSelected={comparisonSelection.some(
                                  (item) =>
                                    comparisonReferenceKey(item.reference) ===
                                    comparisonReferenceKey({
                                      provider_key: offer.provider_key,
                                      provider_product_id: offer.provider_product_id,
                                    }),
                                )}
                                selectionDisabled={
                                  comparisonSelection.length >= 5 &&
                                  !comparisonSelection.some(
                                    (item) =>
                                      comparisonReferenceKey(item.reference) ===
                                      comparisonReferenceKey({
                                        provider_key: offer.provider_key,
                                        provider_product_id: offer.provider_product_id,
                                      }),
                                  )
                                }
                              />
                            )
                          })}
                        </div>
                      ) : (
                        <div className="empty-results">
                          {imageSearchResult.search_query
                            ? 'No offers matched this image search.'
                            : 'No offers are available without a usable search query.'}
                        </div>
                      )}
                    </div>
                  </section>
                )}
              </section>
            )}

            {activeNav === 'My Cart' && (
              <section className="workspace-panel">
                <div className="workspace-page-heading">
                  <div className="eyebrow">YOUR SELECTED ITEMS</div>
                  <h1>My Cart</h1>
                  <p>Review and update the products in your saved cart.</p>
                </div>

                {cartLoading || authStatus === 'loading' ? (
                  <div className="workspace-empty" role="status">
                    <p>Loading your cart...</p>
                  </div>
                ) : authStatus === 'unauthenticated' ? (
                  <div className="workspace-empty">
                    <div className="assistant-welcome-icon">?</div>
                    <h2>Sign in to access your cart</h2>
                    <p>Your cart is saved to your DealSync account.</p>
                    <button className="hero-button" onClick={signIn}>
                      Sign in with Google
                    </button>
                  </div>
                ) : authStatus === 'error' ? (
                  <div className="workspace-empty" role="alert">
                    <p>{cartError}</p>
                    <button className="hero-button" onClick={refreshAuthentication}>
                      Retry
                    </button>
                  </div>
                ) : (
                  <>
                    <div className="cart-tabs" role="tablist" aria-label="Cart sections">
                      <button
                        id="local-cart-tab"
                        type="button"
                        role="tab"
                        data-cart-tab="local"
                        aria-selected={cartSection === 'local'}
                        aria-controls="local-cart-panel"
                        tabIndex={cartSection === 'local' ? 0 : -1}
                        className={`cart-tab ${cartSection === 'local' ? 'selected' : ''}`}
                        onClick={() => setCartSection('local')}
                        onKeyDown={handleCartTabKeyDown}
                      >
                        Local Products
                      </button>
                      <button
                        id="provider-cart-tab"
                        type="button"
                        role="tab"
                        data-cart-tab="provider"
                        aria-selected={cartSection === 'provider'}
                        aria-controls="provider-cart-panel"
                        tabIndex={cartSection === 'provider' ? 0 : -1}
                        className={`cart-tab ${cartSection === 'provider' ? 'selected' : ''}`}
                        onClick={() => setCartSection('provider')}
                        onKeyDown={handleCartTabKeyDown}
                      >
                        Provider Offers
                      </button>
                    </div>

                    <div
                      id="local-cart-panel"
                      role="tabpanel"
                      aria-labelledby="local-cart-tab"
                      tabIndex={0}
                      hidden={cartSection !== 'local'}
                    >
                      {orderSuccess && (
                        <p className="demo-order-success" role="status">
                          {orderSuccess}
                        </p>
                      )}
                      {cartOperation && (
                        <p className="cart-operation-status" role="status">
                          Updating your cart...
                        </p>
                      )}
                      {orderSubmitting && (
                        <p className="cart-operation-status" role="status">
                          Saving your demo order...
                        </p>
                      )}
                      {cartError && (
                        <div className="cart-error" role="alert">
                          <span>{cartError}</span>
                          <button onClick={() => refreshCart()}>Refresh cart</button>
                        </div>
                      )}
                      {cart.length === 0 ? (
                        <div className="workspace-empty">
                          <div className="assistant-welcome-icon">??</div>
                          <h2>Your cart is empty</h2>
                          <p>Visit Products to add items to your cart.</p>
                          <button className="hero-button" onClick={() => setActiveNav('Products')}>
                            Explore products ?
                          </button>
                        </div>
                      ) : (
                        <>
                          {cart.map((item) => (
                            <div className="workspace-cart-row" key={item.product_id}>
                              <div className="workspace-cart-info">
                                <strong>{item.name}</strong>
                                <span>{formatMajorPrice(Number(item.price), item.currency)} each</span>
                              </div>
                              <div className="cart-quantity-controls">
                                <button
                                  type="button"
                                  onClick={() => changeCartQuantity(item, item.quantity - 1)}
                                  disabled={Boolean(cartOperation)}
                                  aria-label={`Decrease ${item.name} quantity`}
                                >
                                  −
                                </button>
                                <span>{item.quantity}</span>
                                <button
                                  type="button"
                                  onClick={() => changeCartQuantity(item, item.quantity + 1)}
                                  disabled={Boolean(cartOperation)}
                                  aria-label={`Increase ${item.name} quantity`}
                                >
                                  +
                                </button>
                              </div>
                              <strong>{formatMajorPrice(Number(item.subtotal), item.currency)}</strong>
                              <button
                                className="text-button"
                                onClick={() => removeCartItem(item.product_id)}
                                disabled={Boolean(cartOperation)}
                                aria-label={`Remove ${item.name} from cart`}
                              >
                                Remove
                              </button>
                            </div>
                          ))}
                          <div className="workspace-cart-total">
                            <span>Subtotal</span>
                            <strong>{cartSubtotalText}</strong>
                            <button
                              className="text-button"
                              onClick={clearCart}
                              disabled={Boolean(cartOperation) || orderSubmitting}
                            >
                              Clear cart
                            </button>
                          </div>
                          <div className="demo-order-actions">
                            <button
                              className="offer-search-button"
                              type="button"
                              onClick={placeDemoOrder}
                              disabled={
                                Boolean(cartOperation) ||
                                orderSubmitting ||
                                cart.length === 0
                              }
                            >
                              {orderSubmitting ? 'Placing demo order...' : 'Place Demo Order'}
                            </button>
                            <p className="demo-note">
                              Demo only: no payment is collected and no purchase is made from
                              Amazon, Flipkart, or another retailer.
                            </p>
                          </div>
                        </>
                      )}
                    </div>

                    <div
                      id="provider-cart-panel"
                      role="tabpanel"
                      aria-labelledby="provider-cart-tab"
                      tabIndex={0}
                      hidden={cartSection !== 'provider'}
                    >
                      {cartSection === 'provider' && (
                        <ExternalCart
                          formatPrice={formatOfferPrice}
                          onSignIn={signIn}
                        />
                      )}
                    </div>
                  </>
                )}
              </section>
            )}

            {activeNav === 'My Orders' && (
              <section className="workspace-panel">
                <div className="workspace-page-heading order-history-heading">
                  <div>
                    <div className="eyebrow">PURCHASE ACTIVITY</div>
                    <h1>My Orders</h1>
                    <p>
                      Your saved demo orders. No payment or retailer purchase is made.
                    </p>
                  </div>
                  {authStatus === 'authenticated' && (
                    <button
                      className="order-refresh-button"
                      type="button"
                      onClick={() => {
                        setOrdersLoading(true)
                        setOrdersError('')
                        setOrdersReload((current) => current + 1)
                      }}
                      disabled={ordersLoading}
                    >
                      {ordersLoading ? 'Refreshing...' : 'Refresh orders'}
                    </button>
                  )}
                </div>
                {authStatus === 'loading' ||
                (authStatus === 'authenticated' && ordersLoading) ? (
                  <div className="workspace-empty" role="status">
                    <p>Loading your order history...</p>
                  </div>
                ) : authStatus === 'unauthenticated' ? (
                  <div className="workspace-empty">
                    <div className="assistant-welcome-icon">?</div>
                    <h2>Sign in to view your orders</h2>
                    <p>Order history is private to your DealSync account.</p>
                    <button className="hero-button" onClick={signIn}>
                      Sign in with Google
                    </button>
                  </div>
                ) : authStatus === 'error' ? (
                  <div className="workspace-empty" role="alert">
                    <p>Could not verify your sign-in. Retry authentication to load your orders.</p>
                    <button className="hero-button" onClick={refreshAuthentication}>
                      Retry
                    </button>
                  </div>
                ) : ordersError ? (
                  <div className="workspace-empty" role="alert">
                    <p>{ordersError}</p>
                    <button
                      className="hero-button"
                      onClick={
                        authStatus === 'error'
                          ? refreshAuthentication
                          : () => {
                              setOrdersLoading(true)
                              setOrdersError('')
                              setOrdersReload((current) => current + 1)
                            }
                      }
                    >
                      Retry
                    </button>
                  </div>
                ) : orders.length === 0 ? (
                  <div className="workspace-empty">
                    <div className="assistant-welcome-icon">▤</div>
                    <h2>No demo orders yet</h2>
                    <p>Place a demo order from your cart to see it here.</p>
                  </div>
                ) : (
                  <div className="demo-order-list">
                    {orders.map((order) => (
                      <article className="demo-order-card" key={order.order_id}>
                        <div className="demo-order-header">
                          <div className="demo-order-heading">
                            <strong>Order {order.order_id}</strong>
                            <span>{formatOrderDate(order.ordered_at)}</span>
                          </div>
                          <span className="demo-order-status">{order.status}</span>
                        </div>
                        <ul className="demo-order-items">
                          {order.items.map((item, index) => (
                            <li key={`${item.product_id ?? item.product_name}-${index}`}>
                              <span>
                                {item.product_name} × {item.quantity}
                              </span>
                              <span>
                                {item.unit_price === null
                                  ? 'Price unavailable'
                                  : `${formatMajorPrice(item.unit_price, item.currency)} each`}
                              </span>
                            </li>
                          ))}
                        </ul>
                        <div className="demo-order-total">
                          <span>Total</span>
                          <strong>
                            {order.total_minor === null || order.currency === null
                              ? 'Currency unverified or mixed'
                              : formatOfferPrice(order.total_minor, order.currency)}
                          </strong>
                        </div>
                      </article>
                    ))}
                  </div>
                )}
              </section>
            )}

            <footer className="footer">
              <span>? 2026 DealSync ? Smart shopping, simplified.</span>
              <span>Built for better discoveries <b>?</b></span>
            </footer>
          </div>
        </main>
      </div>
    )
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">D</div>
          <div>
            <span className="brand-name">DealSync</span>
            <span className="brand-caption">SMART SHOPPING</span>
          </div>
        </div>

        <div className="nav-heading">WORKSPACE</div>
        <nav className="nav-list" aria-label="Main navigation">
          {navItems.map((item) => (
            <button
              key={item.label}
              className={`nav-item ${activeNav === item.label ? 'active' : ''}`}
              onClick={() => navigateTo(item.label)}
              aria-label={
                item.label === 'Compare'
                  ? `Compare products, ${comparisonSelection.length} selected`
                  : undefined
              }
            >
              <span className="nav-icon">{item.icon}</span>
              <span>{item.label}</span>
              {item.label === 'My Cart' && cartCount > 0 && (
                <span className="nav-count">{cartCount}</span>
              )}
              {item.label === 'Compare' && comparisonSelection.length > 0 && (
                <span className="nav-count">{comparisonSelection.length}</span>
              )}
            </button>
          ))}
        </nav>

      </aside>

      <main className="main-area">
        <header className="topbar">
          <div className="breadcrumb">Workspace <span>/</span> {activeNav}</div>
          <div className="topbar-actions">
            <span className="preview-label"><span className="status-dot" /> Frontend preview</span>
            <button className="icon-button" aria-label="Notifications">♧</button>
            <AccountControl
              key={authStatus === 'authenticated' ? 'signed-in' : 'signed-out'}
              isAuthenticated={authStatus === 'authenticated'}
              signOutPending={signOutPending}
              signOutError={signOutError}
              onSignIn={signIn}
              onSignOut={signOut}
            />
          </div>
        </header>

        <div className="page-content">
          <section className="welcome-row">
            <div>
              <div className="eyebrow">YOUR PERSONAL SHOPPING SPACE</div>
              <h1>Find better deals. <span>Shop smarter.</span></h1>
              <p className="welcome-subtitle">Discover products, compare prices and make smarter shopping decisions.</p>
            </div>
            <div className="date-chip">✧ &nbsp; AI-powered shopping</div>
          </section>

          <section className="hero-banner">
            <div className="hero-copy">
              <div className="hero-kicker"><span>✦</span> MEET YOUR SHOPPING ASSISTANT</div>
              <h2>Good choices<br />start with <span>DealSync.</span></h2>
              <p>Tell us what you need. Your shopping assistant will help you explore the possibilities.</p>
              <button className="hero-button" onClick={() => setActiveNav('AI Assistant')}>
                Explore AI Assistant <span>→</span>
              </button>
            </div>
            <div className="hero-art" aria-hidden="true">
              <div className="orbit orbit-one" />
              <div className="orbit orbit-two" />
              <div className="hero-circle">✦</div>
              <div className="floating-card card-top">✧ &nbsp; Smart recommendations</div>
              <div className="floating-card card-bottom">✓ &nbsp; Better shopping choices</div>
              <div className="sparkle sparkle-one">✦</div>
              <div className="sparkle sparkle-two">✧</div>
            </div>
          </section>

          <section className="stats-grid">
            <button
              className="stat-card"
              type="button"
              aria-label="Smart Search: open Products and focus product search"
              onClick={() => {
                pendingProductSearchFocusRef.current = true
                navigateTo('Products')
              }}
            >
              <div className="stat-icon purple">⌕</div>
              <div><span className="stat-label">PRODUCT DISCOVERY</span><strong>Smart search</strong><small>Find what you need</small></div>
            </button>
            <button
              className="stat-card"
              type="button"
              aria-label="Compare Price: open product comparison"
              onClick={() => navigateTo('Compare')}
            >
              <div className="stat-icon green">↗</div>
              <div><span className="stat-label">DEAL COMPARISON</span><strong>Compare prices</strong><small>Explore better value</small></div>
            </button>
            <button
              className="stat-card"
              type="button"
              aria-label="Personal Help: open the AI shopping assistant"
              onClick={() => navigateTo('AI Assistant')}
            >
              <div className="stat-icon orange">✧</div>
              <div><span className="stat-label">AI ASSISTANCE</span><strong>Personal help</strong><small>Shopping made easier</small></div>
            </button>
          </section>

          <div className="content-grid">
            <section className="products-section">
              <div className="section-heading">
                <div>
                  <div className="eyebrow">FROM YOUR PRODUCT CATALOG</div>
                  <h2>Explore products</h2>
                </div>
                <button className="text-button" onClick={() => { setActiveNav('Products'); setCategory('All'); setQuery('') }}>
                  View all <span>→</span>
                </button>
              </div>

              <div className="search-box">
                <span className="search-icon">⌕</span>
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search products..."
                  aria-label="Search products"
                />
                <span className="search-shortcut">SEARCH</span>
              </div>

              <div className="category-row">
                {categoryOptions.map((item) => (
                  <button
                    key={item}
                    className={`category-chip ${category === item ? 'selected' : ''}`}
                    onClick={() => setCategory(item)}
                  >
                    {item}
                  </button>
                ))}
              </div>

              {productsLoading && (
                <div className="empty-results" role="status">
                  Loading products from your database...
                </div>
              )}
              {productsError && (
                <div className="empty-results" role="alert">
                  {productsError}
                </div>
              )}
              {!productsLoading && !productsError && (
                <>
                  <div className="product-grid">
                    {filteredProducts.slice(0, 4).map((product) => (
                      <ProductCard
                        key={product.id}
                        product={product}
                        onAdd={addToCart}
                        canAdd={authStatus === 'authenticated'}
                        isAdding={cartOperation === `add-${product.id}`}
                      />
                    ))}
                    {filteredProducts.length === 0 && (
                      <div className="empty-results">
                        {apiProducts.length === 0
                          ? 'No products are available in the catalog.'
                          : 'No matching products found. Try another search or category.'}
                      </div>
                    )}
                  </div>
                  <p className="demo-note">
                    Showing {Math.min(filteredProducts.length, 4)} of {filteredProducts.length} matching database products.
                  </p>
                </>
              )}
            </section>

            <aside className="assistant-panel">
              <div className="assistant-heading">
                <div className="assistant-symbol">✧</div>
                <div><h2>AI Assistant</h2><span><i /> Shopping assistant</span></div>
                <button className="more-button" aria-label="More options">•••</button>
              </div>
              <div className="assistant-intro">
                <div className="assistant-welcome-icon">✦</div>
                <h3>Your shopping companion</h3>
                <p>Ask questions and get shopping help from your DealSync assistant.</p>
              </div>
              <div className="chat-messages" aria-live="polite">
                {messages.map((message, index) => (
                  <div className={`chat-message ${message.from}`} key={index}>
                    {message.from === 'assistant' && <span className="message-avatar">✧</span>}
                    {message.from === 'assistant' ? (
                      <div className="message-content">
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>
                          {message.text}
                        </ReactMarkdown>
                        {message.cartPreview && (
                          <CartPreviewCard
                            preview={message.cartPreview}
                            canConfirm={
                              Boolean(message.cartConfirmationToken) &&
                              message.cartConfirmationToken === activeCartConfirmationToken
                            }
                            isLoading={isChatLoading}
                            onConfirm={() =>
                              confirmCartPreview(message.cartConfirmationToken)
                            }
                          />
                        )}
                      </div>
                    ) : (
                      <p>{message.text}</p>
                    )}
                  </div>
                ))}
                {isChatLoading && (
                  <div className="chat-message assistant" role="status">
                    <span className="message-avatar">✧</span>
                    <p className="message-content">Thinking...</p>
                  </div>
                )}
              </div>
              {chatError && <p className="chat-error" role="alert">{chatError}</p>}
              <div className="suggestion-label">TRY ASKING</div>
              <div className="suggestions">
                <button onClick={() => setChatInput('Find affordable headphones')}>Affordable headphones</button>
                <button onClick={() => setChatInput('Help me compare products')}>Compare products</button>
              </div>
              <form className="chat-form" onSubmit={sendMessage}>
                <input
                  value={chatInput}
                  onChange={(event) => setChatInput(event.target.value)}
                  placeholder="Ask me anything..."
                  aria-label="Message the shopping assistant"
                  disabled={isChatLoading}
                />
                <button type="submit" aria-label="Send message" disabled={isChatLoading}>↑</button>
              </form>
              <p className="chat-disclaimer">AI responses are generated by the DealSync shopping assistant.</p>
            </aside>
          </div>

          <section className="cart-strip">
            <div className="cart-strip-icon">🛒</div>
            <div className="cart-strip-copy">
              <strong>Your shopping cart</strong>
              <span>
                {authStatus === 'loading' || cartLoading
                  ? 'Loading your cart...'
                  : authStatus === 'unauthenticated'
                    ? 'Sign in to save and access your cart.'
                    : `${cartCount} item(s) in your saved cart`}
              </span>
            </div>
            <div className="cart-strip-total">
              <span>SUBTOTAL</span>
              <strong>{cartSubtotalText}</strong>
            </div>
            {authStatus === 'unauthenticated' ? (
              <button className="cart-view-button" onClick={signIn}>
                Sign in with Google
              </button>
            ) : (
              <button className="cart-view-button" onClick={() => setActiveNav('My Cart')}>
                View cart →
              </button>
            )}
          </section>
          {cartError && activeNav === 'Dashboard' && (
            <div className="cart-error" role="alert">
              <span>{cartError}</span>
              <button onClick={() => refreshCart()}>Refresh cart</button>
            </div>
          )}

          <footer className="footer">
            <span>© 2026 DealSync · Smart shopping, simplified.</span>
            <span>Built for better discoveries <b>✦</b></span>
          </footer>
        </div>
      </main>
    </div>
  )
}

export default App
