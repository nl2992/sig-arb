const request = async (path, options = {}) => {
  const response = await fetch(path, {signal: AbortSignal.timeout(120000), ...options});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `Request failed: ${response.status}`);
  return data;
};

export const getOpportunities = () => request('/api/opportunities');
export const getOpportunity = id => request(`/api/opportunities/${encodeURIComponent(id)}`);
export const getBook = (venue, marketId) => request(`/api/books/${encodeURIComponent(venue)}/${encodeURIComponent(marketId)}`);
export const getHistory = marketId => request(`/api/history/${encodeURIComponent(marketId)}`);
