import { redirect } from "next/navigation";
import { getServerSession } from "next-auth";
import { authOptions } from "@/lib/auth-options";
import { ChatInterface } from "../../components/chat-interface";

export const metadata = {
  title: "Chat — BPPIMT Campus Assistant",
};

export default async function ChatPage() {
  const session = await getServerSession(authOptions);
  if (!session) redirect("/login");

  return <ChatInterface />;
}
