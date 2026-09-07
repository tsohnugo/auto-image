export default function StageBadge({ stage }) {
  return <span className={`va-art-badge s-${stage.toLowerCase()}`}>{stage}</span>
}
