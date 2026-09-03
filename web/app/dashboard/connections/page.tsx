import ConnectionsScreen from "@/components/screens/ConnectionsScreen";

export default function ConnectionsPage() {
  const mcpUrl = process.env.PURSE_MCP_URL || "https://your-vault.dev/mcp";
  return <ConnectionsScreen mcpUrl={mcpUrl} />;
}
