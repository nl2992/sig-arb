import {getOpportunity} from './api.js';

export const openOpportunityDrawer = async (id, render) => {
  render({loading: true});
  try { render(await getOpportunity(id)); }
  catch (error) { render({error}); }
};
