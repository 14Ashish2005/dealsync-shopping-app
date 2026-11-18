import { useCallback, useEffect, useRef, useState } from 'react'

const QUOTE_STATUS_LABELS = {
  verified_current: 'Current quote',
  stale_unverified: 'Stale — not revalidated',
  unavailable: 'Unavailable',
  price_change_pending: 'Price change pending',
}

const AVAILABILITY_LABELS = {
  in_stock: 'In stock',
  out_of_stock: 'Out of stock',
  preorder: 'Preorder',
  backorder: 'Backorder',
  unknown: 'Unknown',
}

const AVAILABILITY_VALUES = new Set(Object.keys(AVAILABILITY_LABELS))
const QUOTE_STATUS_VALUES = new Set(Object.keys(QUOTE_STATUS_LABELS))

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isNullableString(value) {
  return value === null || typeof value === 'string'
}

function isNullableSafeInteger(value) {
  return value === null || Number.isSafeInteger(value)
}

function isExternalCartResponse(value) {
  if (
    !isRecord(value) ||
    !Array.isArray(value.items) ||
    !Array.isArray(value.currency_totals)
  ) {
    return false
  }

  const validItems = value.items.every(
    (item) =>
      isRecord(item) &&
      typeof item.offer_id === 'string' &&
      typeof item.provider_key === 'string' &&
      typeof item.provider_offer_key === 'string' &&
      typeof item.provider_product_id === 'string' &&
      typeof item.product_name === 'string' &&
      Number.isSafeInteger(item.price_minor) &&
      item.price_minor >= 0 &&
      typeof item.currency === 'string' &&
      Number.isSafeInteger(item.quantity) &&
      item.quantity > 0 &&
      Number.isSafeInteger(item.snapshot_subtotal_minor) &&
      item.snapshot_subtotal_minor >= 0 &&
      isNullableString(item.product_url) &&
      AVAILABILITY_VALUES.has(item.availability) &&
      isNullableString(item.verified_at) &&
      typeof item.added_at === 'string' &&
      QUOTE_STATUS_VALUES.has(item.quote_status) &&
      isNullableSafeInteger(item.pending_price_minor) &&
      isNullableString(item.pending_currency) &&
      isNullableString(item.pending_verified_at) &&
      isNullableString(item.pending_product_name) &&
      isNullableString(item.pending_product_url) &&
      (item.pending_availability === null ||
        AVAILABILITY_VALUES.has(item.pending_availability)),
  )

  const validTotals = value.currency_totals.every(
    (total) =>
      isRecord(total) &&
      typeof total.currency === 'string' &&
      Number.isSafeInteger(total.snapshot_total_minor) &&
      total.snapshot_total_minor >= 0 &&
      Number.isSafeInteger(total.item_count) &&
      total.item_count >= 0 &&
      typeof total.all_quotes_current === 'boolean',
  )

  return validItems && validTotals
}

function validListingUrl(value) {
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

function formatTimestamp(value) {
  if (typeof value !== 'string') return null

  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return null

  return (
    <time dateTime={value}>
      {date.toLocaleString()}
    </time>
  )
}

function createHttpError(status) {
  const error = new Error(`Request failed with HTTP ${status}.`)
  error.status = status
  return error
}

function createInvalidResponseError() {
  const error = new Error('The provider-offer service returned an unexpected response.')
  error.kind = 'invalid-response'
  return error
}

async function requestExternalCart(signal) {
  const response = await fetch('/api/cart/external', {
    method: 'GET',
    credentials: 'same-origin',
    signal,
  })

  if (!response.ok) throw createHttpError(response.status)

  let data
  try {
    data = await response.json()
  } catch {
    throw createInvalidResponseError()
  }

  if (!isExternalCartResponse(data)) throw createInvalidResponseError()
  return data
}

function getErrorState(error) {
  if (error.status === 401) return 'unauthenticated'
  if (error.status === 503) return 'unavailable'
  if (error.kind === 'invalid-response') return 'invalid-response'
  if (error instanceof TypeError) return 'network-error'
  return 'error'
}

function getErrorMessage(error) {
  const statusMessages = {
    401: 'Your session has expired. Sign in again to view provider offers.',
    403: 'The request was blocked by origin protection. Reload the page and retry.',
    404: 'The offer or cart item was not found.',
    409: 'The offer is stale, unavailable, or requires confirmation. Review its current status.',
    422: 'The request was rejected as invalid.',
    503: 'The external-cart or provider service is unavailable.',
    504: 'Provider quote resolution timed out.',
  }

  if (statusMessages[error.status]) return statusMessages[error.status]
  if (error.kind === 'invalid-response') {
    return 'The provider-offer service returned an unexpected response.'
  }
  if (error instanceof TypeError) {
    return 'Could not reach the provider-offer service. Check that the backend is running.'
  }
  return error.status
    ? `The request failed (HTTP ${error.status}).`
    : 'Could not complete the request.'
}

export default function ExternalCart({ formatPrice, onSignIn }) {
  const [cart, setCart] = useState(null)
  const [loadState, setLoadState] = useState('loading')
  const [loadError, setLoadError] = useState('')
  const [operation, setOperation] = useState('')
  const [mutationMessage, setMutationMessage] = useState('')
  const [mutationMessageIsError, setMutationMessageIsError] = useState(false)
  const [clearConfirmation, setClearConfirmation] = useState(false)
  const [removeConfirmationOfferId, setRemoveConfirmationOfferId] = useState(null)
  const [retryCount, setRetryCount] = useState(0)
  const mutationInFlightRef = useRef(false)

  const reloadCart = useCallback(async (signal, showLoading = true) => {
    if (showLoading) setLoadState('loading')

    try {
      const latestCart = await requestExternalCart(signal)
      setCart(latestCart)
      setLoadState('loaded')
      setLoadError('')
      return true
    } catch (error) {
      if (signal?.aborted) return false
      setLoadState(getErrorState(error))
      setLoadError(getErrorMessage(error))
      return false
    }
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    requestExternalCart(controller.signal)
      .then((latestCart) => {
        if (controller.signal.aborted) return
        setCart(latestCart)
        setLoadState('loaded')
        setLoadError('')
      })
      .catch((error) => {
        if (controller.signal.aborted) return
        setLoadState(getErrorState(error))
        setLoadError(getErrorMessage(error))
      })

    return () => controller.abort()
  }, [retryCount])

  async function performMutation(label, url, options = {}) {
    if (mutationInFlightRef.current) return

    mutationInFlightRef.current = true
    setOperation(label)
    setMutationMessage('')
    setMutationMessageIsError(false)

    try {
      const response = await fetch(url, {
        ...options,
        credentials: 'same-origin',
      })
      if (!response.ok) throw createHttpError(response.status)

      const reloaded = await reloadCart(undefined, false)
      if (reloaded) {
        setMutationMessage(`${label} completed. Cart state was reloaded from the server.`)
      } else {
        setMutationMessage(
          `${label} completed, but the cart could not be reloaded. Retry loading before making another change.`,
        )
        setMutationMessageIsError(true)
      }
    } catch (error) {
      const errorMessage = getErrorMessage(error)
      const isNetworkError = error instanceof TypeError
      const reloaded = await reloadCart(undefined, false)

      if (reloaded) {
        setMutationMessage(
          isNetworkError
            ? `The result of ${label.toLowerCase()} could not be confirmed because of a network error. The cart was reloaded; check its current state before retrying.`
            : `${errorMessage} The cart was reloaded from the server.`,
        )
      } else {
        setMutationMessage(
          `${isNetworkError ? 'The result could not be confirmed because of a network error.' : errorMessage} Cart state could not be reloaded; retry loading before making another change.`,
        )
      }
      setMutationMessageIsError(true)
    } finally {
      mutationInFlightRef.current = false
      setOperation('')
    }
  }

  function updateQuantity(item, quantity) {
    if (!Number.isSafeInteger(quantity) || quantity < 1) {
      setMutationMessage('Quantity must be a positive whole number.')
      setMutationMessageIsError(true)
      return
    }

    void performMutation(
      'Updating quantity',
      `/api/cart/external/items/${encodeURIComponent(item.offer_id)}`,
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ quantity }),
      },
    )
  }

  function removeItem(offerId) {
    setRemoveConfirmationOfferId(null)
    void performMutation(
      'Removing provider offer',
      `/api/cart/external/items/${encodeURIComponent(offerId)}`,
      { method: 'DELETE' },
    )
  }

  function clearCart() {
    setClearConfirmation(false)
    void performMutation(
      'Clearing Provider Offers',
      '/api/cart/external',
      { method: 'DELETE' },
    )
  }

  function refreshQuote(offerId) {
    void performMutation(
      'Refreshing quote',
      `/api/cart/external/items/${encodeURIComponent(offerId)}/refresh`,
      { method: 'POST' },
    )
  }

  function confirmPrice(offerId) {
    void performMutation(
      'Confirming proposed price',
      `/api/cart/external/items/${encodeURIComponent(offerId)}/confirm-price`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm: true }),
      },
    )
  }

  if (loadState === 'loading') {
    return (
      <div className="external-cart-message" role="status">
        Loading provider offers...
      </div>
    )
  }

  if (loadState === 'unauthenticated') {
    return (
      <div className="external-cart-message" role="alert">
        <h2>Sign in to view provider offers</h2>
        <p>Your provider-offer cart is available only to your DealSync account.</p>
        {mutationMessage && (
          <p className="external-cart-operation-error">{mutationMessage}</p>
        )}
        <button className="hero-button" type="button" onClick={onSignIn}>
          Sign in with Google
        </button>
      </div>
    )
  }

  if (loadState !== 'loaded') {
    return (
      <div className="external-cart-message" role="alert">
        <p>{loadError || 'Could not load provider offers.'}</p>
        {mutationMessage && (
          <p className="external-cart-operation-error">{mutationMessage}</p>
        )}
        <button
          className="text-button external-cart-retry"
          type="button"
          onClick={() => {
            setMutationMessage('')
            setLoadState('loading')
            setRetryCount((count) => count + 1)
          }}
        >
          Retry loading cart
        </button>
      </div>
    )
  }

  if (cart.items.length === 0) {
    return (
      <div className="external-cart-message" role="status">
        {operation && (
          <p className="external-cart-operation">
            {operation}...
          </p>
        )}
        {mutationMessage && (
          <p
            className={
              mutationMessageIsError
                ? 'external-cart-operation-error'
                : 'external-cart-operation-success'
            }
            role={mutationMessageIsError ? 'alert' : 'status'}
          >
            {mutationMessage}
          </p>
        )}
        <h2>No provider offers yet</h2>
        <p>
          No eligible external provider is currently enabled. Local catalog
          products cannot be added to this cart.
        </p>
      </div>
    )
  }

  return (
    <div className="external-cart">
      {operation && (
        <p className="external-cart-operation" role="status">
          {operation}...
        </p>
      )}
      {mutationMessage && (
        <p
          className={
            mutationMessageIsError
              ? 'external-cart-operation-error'
              : 'external-cart-operation-success'
          }
          role={mutationMessageIsError ? 'alert' : 'status'}
        >
          {mutationMessage}
        </p>
      )}

      <div className="external-cart-toolbar">
        <p>
          Provider Offers are separate from Local Products. Clearing this
          section never changes the Local Products cart.
        </p>
        {cart.items.length > 0 && (
          <div className="external-cart-clear">
            {!clearConfirmation ? (
              <button
                className="external-cart-danger-button"
                type="button"
                disabled={Boolean(operation)}
                onClick={() => setClearConfirmation(true)}
              >
                Clear Provider Offers
              </button>
            ) : (
              <div
                className="external-cart-confirmation"
                role="group"
                aria-label="Confirm clearing Provider Offers"
              >
                <p>
                  Clear all Provider Offers? Local Products will remain
                  unchanged.
                </p>
                <button
                  className="external-cart-danger-button"
                  type="button"
                  disabled={Boolean(operation)}
                  onClick={clearCart}
                >
                  Confirm clear
                </button>
                <button
                  className="text-button"
                  type="button"
                  disabled={Boolean(operation)}
                  onClick={() => setClearConfirmation(false)}
                >
                  Cancel
                </button>
              </div>
            )}
          </div>
        )}
      </div>

      <div className="external-cart-items" aria-label="Provider offers in your cart">
        {cart.items.map((item) => {
          const listingUrl =
            item.provider_key === 'demo_catalog'
              ? null
              : validListingUrl(item.product_url)
          const verificationTime = formatTimestamp(item.verified_at)
          const proposedQuoteTime = formatTimestamp(item.pending_verified_at)
          const isPending = item.quote_status === 'price_change_pending'
          const hasProposedPrice =
            Number.isSafeInteger(item.pending_price_minor) &&
            item.pending_price_minor >= 0 &&
            typeof item.pending_currency === 'string'

          return (
            <article className="external-cart-item" key={item.offer_id}>
              <div className="external-cart-item-heading">
                <div>
                  <h2>{item.product_name}</h2>
                  <span className="external-cart-provider">
                    Provider: {item.provider_key}
                  </span>
                </div>
                <span
                  className={`external-cart-status status-${item.quote_status}`}
                >
                  {QUOTE_STATUS_LABELS[item.quote_status]}
                </span>
              </div>

              <dl className="external-cart-item-details">
                <div>
                  <dt>Price</dt>
                  <dd>{formatPrice(item.price_minor, item.currency)}</dd>
                </div>
                <div>
                  <dt>Quantity</dt>
                  <dd>{item.quantity}</dd>
                </div>
                <div>
                  <dt>Availability</dt>
                  <dd>{AVAILABILITY_LABELS[item.availability]}</dd>
                </div>
                <div>
                  <dt>Quote status</dt>
                  <dd>{QUOTE_STATUS_LABELS[item.quote_status]}</dd>
                </div>
                <div>
                  <dt>Snapshot subtotal</dt>
                  <dd>
                    {formatPrice(item.snapshot_subtotal_minor, item.currency)}
                  </dd>
                </div>
                {verificationTime && (
                  <div>
                    <dt>Verification</dt>
                    <dd>{verificationTime}</dd>
                  </div>
                )}
              </dl>

              {isPending && (
                <div className="external-cart-pending" role="status">
                  <strong>Price change pending</strong>
                  <span>
                    Selected snapshot:{' '}
                    {formatPrice(item.price_minor, item.currency)}
                  </span>
                  <span>
                    Proposed price and currency:{' '}
                    {hasProposedPrice
                      ? formatPrice(
                          item.pending_price_minor,
                          item.pending_currency,
                        )
                      : 'Price unavailable'}
                  </span>
                  {proposedQuoteTime && (
                    <span>
                      Proposed quote timestamp: {proposedQuoteTime}
                    </span>
                  )}
                  <p>
                    The selected snapshot has not been changed. Confirmation
                    does not refresh quote verification; an expired proposal
                    may remain marked stale after acceptance.
                  </p>
                  {hasProposedPrice && (
                    <button
                      className="external-cart-confirm-button"
                      type="button"
                      disabled={Boolean(operation)}
                      onClick={() => confirmPrice(item.offer_id)}
                    >
                      Confirm proposed price
                    </button>
                  )}
                </div>
              )}

              <div className="external-cart-actions">
                <div
                  className="external-cart-quantity-controls"
                  aria-label={`Quantity controls for ${item.product_name}`}
                >
                  <button
                    type="button"
                    disabled={Boolean(operation) || item.quantity <= 1}
                    onClick={() => updateQuantity(item, item.quantity - 1)}
                    aria-label={`Decrease ${item.product_name} quantity`}
                  >
                    −
                  </button>
                  <span aria-live="polite">{item.quantity}</span>
                  <button
                    type="button"
                    disabled={
                      Boolean(operation) ||
                      item.quantity >= Number.MAX_SAFE_INTEGER
                    }
                    onClick={() => updateQuantity(item, item.quantity + 1)}
                    aria-label={`Increase ${item.product_name} quantity`}
                  >
                    +
                  </button>
                </div>
                <button
                  className="external-cart-secondary-button"
                  type="button"
                  disabled={Boolean(operation) || isPending}
                  title={
                    isPending
                      ? 'Confirm or remove the pending price change before refreshing.'
                      : undefined
                  }
                  onClick={() => refreshQuote(item.offer_id)}
                >
                  Refresh quote
                </button>
                {!removeConfirmationOfferId ||
                removeConfirmationOfferId !== item.offer_id ? (
                  <button
                    className="external-cart-secondary-button"
                    type="button"
                    disabled={Boolean(operation)}
                    onClick={() =>
                      setRemoveConfirmationOfferId(item.offer_id)
                    }
                  >
                    Remove offer
                  </button>
                ) : (
                  <div className="external-cart-remove-confirmation">
                    <span>Remove this provider offer?</span>
                    <button
                      className="external-cart-danger-button"
                      type="button"
                      disabled={Boolean(operation)}
                      onClick={() => removeItem(item.offer_id)}
                    >
                      Confirm remove
                    </button>
                    <button
                      className="text-button"
                      type="button"
                      disabled={Boolean(operation)}
                      onClick={() => setRemoveConfirmationOfferId(null)}
                    >
                      Cancel
                    </button>
                  </div>
                )}
              </div>

              {listingUrl && (
                <a
                  className="external-cart-listing"
                  href={listingUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  View provider listing
                </a>
              )}
            </article>
          )
        })}
      </div>

      {cart.currency_totals.length > 0 && (
        <section className="external-cart-totals" aria-label="Totals by currency">
          <h2>Totals by currency</h2>
          <ul>
            {cart.currency_totals.map((total) => (
              <li key={total.currency}>
                <strong>
                  {formatPrice(total.snapshot_total_minor, total.currency)}
                </strong>
                <span>
                  {total.item_count}{' '}
                  {total.item_count === 1 ? 'item' : 'items'}
                </span>
                <span>
                  {total.all_quotes_current
                    ? 'All quotes current'
                    : 'One or more quotes are not current'}
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}
